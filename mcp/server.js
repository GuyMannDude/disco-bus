#!/usr/bin/env node
/**
 * Disco-Bus MCP server
 *
 * Exposes three tools — `ping` (send), `ping_history` (list summaries),
 * and `ping_read` (fetch full envelope including body) — that wrap the
 * Disco-Bus dispatcher's HTTP API. Stdio MCP transport for use with
 * Claude Code, Claude Desktop, and other MCP clients.
 *
 * Server-bound identity: AGENT_ID comes from DISCOBUS_AGENT env. Caller
 * cannot spoof `from`.
 *
 * Env:
 *   DISCOBUS_AGENT        agent name this MCP instance speaks as (required)
 *   DISCOBUS_DISPATCHER   dispatcher base URL (default http://127.0.0.1:9100)
 *   DISCOBUS_AGENTS_FILE  path to agents.json registry
 *                         (default ~/.disco-bus/agents.json)
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const AGENT_ID = process.env.DISCOBUS_AGENT || "unknown";
const DISPATCHER = process.env.DISCOBUS_DISPATCHER || "http://127.0.0.1:9100";
const AGENTS_FILE = process.env.DISCOBUS_AGENTS_FILE || join(homedir(), ".disco-bus", "agents.json");
const MESH_VERSION = "0.5";

function loadKnownAgents() {
  try {
    const raw = readFileSync(AGENTS_FILE, "utf8");
    return Object.keys(JSON.parse(raw));
  } catch {
    return [];
  }
}

const KNOWN_AGENTS = loadKnownAgents();
const peerList = KNOWN_AGENTS.filter((a) => a !== AGENT_ID).join(", ") || "(none registered yet)";

const server = new McpServer({
  name: "disco-bus",
  version: "0.1.0",
  description:
    `Disco-Bus — push-based agent mesh. You are ${AGENT_ID}. ` +
    `Send pings via 'ping'. List recent traffic via 'ping_history'. ` +
    `Read a specific message's full body via 'ping_read'. ` +
    `List messages addressed to you via 'inbox'. ` +
    `Walk a full reply chain via 'thread'. ` +
    `Other agents: ${peerList}.`,
});

// --- ping ---
server.tool(
  "ping",
  `Send a pushed message to another agent via the dispatcher. ` +
    `Target agent's listener is woken immediately. Replies use reply_to.`,
  {
    to: z
      .string()
      .describe(
        KNOWN_AGENTS.length
          ? `Target agent: ${KNOWN_AGENTS.join(", ")}`
          : "Target agent (must exist in agents.json registry)"
      ),
    subject: z
      .string()
      .min(1)
      .max(200)
      .describe("Short subject. Convention: kebab-case under 80 chars."),
    body: z
      .string()
      .describe("Body as JSON string. Free-form payload — file paths, specs, results."),
    reply_to: z
      .number()
      .int()
      .min(1)
      .optional()
      .describe("Parent message id if this is a reply. Omit for new threads."),
  },
  async ({ to, subject, body, reply_to }) => {
    if (KNOWN_AGENTS.length && !KNOWN_AGENTS.includes(to)) {
      return {
        content: [
          {
            type: "text",
            text: `Unknown agent "${to}". Registered: ${KNOWN_AGENTS.join(", ")}`,
          },
        ],
      };
    }
    let parsedBody;
    try {
      parsedBody = JSON.parse(body);
      if (typeof parsedBody !== "object" || parsedBody === null || Array.isArray(parsedBody)) {
        parsedBody = { text: body };
      }
    } catch {
      parsedBody = { text: body };
    }

    const envelope = {
      mesh_version: MESH_VERSION,
      from: AGENT_ID,
      to,
      subject,
      body: parsedBody,
    };
    if (reply_to !== undefined) envelope.reply_to = reply_to;

    try {
      const r = await fetch(`${DISPATCHER}/mesh/ping`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(envelope),
      });
      const data = await r.json();
      if (!r.ok) {
        return {
          content: [
            {
              type: "text",
              text: `Dispatcher rejected (HTTP ${r.status}): ${JSON.stringify(data)}`,
            },
          ],
        };
      }
      // A HELD receipt must not read like a sent one: HELD means this agent
      // is paused and the message was NOT delivered (dispatcher pause, v0.8).
      const heldNote =
        data.state === "HELD"
          ? ` — NOT DELIVERED: you are paused; the draft is held on the dispatcher until '\\bus play' (or /mesh/release/${data.id} for a hot item, announced in the open)`
          : "";
      return {
        content: [
          {
            type: "text",
            text:
              `Ping #${data.id} accepted: ${AGENT_ID}>${to} ` +
              `subject="${subject}" state=${data.state} ` +
              `tracking_id=${data.tracking_id}${heldNote}`,
          },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text",
            text: `Dispatcher unreachable at ${DISPATCHER}: ${e.message}`,
          },
        ],
      };
    }
  }
);

// --- ping_history ---
server.tool(
  "ping_history",
  `Recent envelopes from the bus (newest first). For debugging delivery state.`,
  {
    limit: z
      .number()
      .int()
      .min(1)
      .max(500)
      .optional()
      .default(50)
      .describe("Max envelopes to return (default 50, max 500)."),
  },
  async ({ limit }) => {
    try {
      const r = await fetch(`${DISPATCHER}/mesh/history?limit=${limit}`);
      const data = await r.json();
      if (!r.ok) {
        return {
          content: [
            { type: "text", text: `Dispatcher error (HTTP ${r.status}): ${JSON.stringify(data)}` },
          ],
        };
      }
      const lines = data.map(
        (m) =>
          `#${m.id} ${m.from}>${m.to} state=${m.state} ` +
          `reply_to=${m.reply_to ?? "null"} subject=${JSON.stringify(m.subject)}`
      );
      return {
        content: [
          {
            type: "text",
            text: lines.length ? lines.join("\n") : "(no envelopes)",
          },
        ],
      };
    } catch (e) {
      return {
        content: [
          { type: "text", text: `Dispatcher unreachable at ${DISPATCHER}: ${e.message}` },
        ],
      };
    }
  }
);

// --- ping_read ---
server.tool(
  "ping_read",
  `Fetch the full envelope (including body) for a specific message id. ` +
    `Use when ping_history shows a message you need to read in detail. ` +
    `This call MARKS the message read, so it always returns a read_at — ` +
    `check first_read (and the leading banner) to tell whether you are the ` +
    `first to open it. A populated read_at is NOT by itself evidence you ` +
    `already saw the message.`,
  {
    id: z
      .number()
      .int()
      .min(1)
      .describe("Envelope id from ping_history."),
  },
  async ({ id }) => {
    try {
      const r = await fetch(`${DISPATCHER}/mesh/read/${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent: AGENT_ID }),
      });
      const data = await r.json();
      if (!r.ok) {
        return {
          content: [
            { type: "text", text: `Dispatcher error (HTTP ${r.status}): ${JSON.stringify(data)}` },
          ],
        };
      }
      // read_at alone is ambiguous: this call sets it, so a first read and a
      // hundredth read return the same field. Lead with the verdict.
      const banner = data.first_read
        ? `NEW — you are the first to open #${id}. The read_at below was set by ` +
          `THIS call, so it is not evidence anyone saw this before you. Treat the ` +
          `message as unread-until-now and act on it.`
        : `ALREADY READ — #${id} was opened at ${data.read_at}, before this call.`;
      return {
        content: [{ type: "text", text: `${banner}\n\n${JSON.stringify(data, null, 2)}` }],
      };
    } catch (e) {
      return {
        content: [{ type: "text", text: `Dispatcher unreachable at ${DISPATCHER}: ${e.message}` }],
      };
    }
  }
);

// --- inbox ---
server.tool(
  "inbox",
  `List messages addressed to an agent (default: this agent). Use filter="unread" ` +
    `for messages not yet opened, filter="unreplied" for messages not yet replied ` +
    `to, or filter="all". Legacy unread_only=true means "unreplied". Returns summary form ` +
    `like ping_history — call ping_read(id) on anything you want to read fully.`,
  {
    agent: z
      .string()
      .optional()
      .describe(`Agent whose inbox to read. Defaults to this MCP instance's agent (${AGENT_ID}).`),
    limit: z
      .number()
      .int()
      .min(1)
      .max(500)
      .optional()
      .default(50)
      .describe("Max messages to return (default 50, max 500)."),
    unread_only: z
      .boolean()
      .optional()
      .default(false)
      .describe("If true, only return messages with no reply from the recipient."),
    filter: z
      .enum(["unread", "unreplied", "all"])
      .optional()
      .describe(
        'Inbox filter. "unread" checks read_at; "unreplied" checks reply chains; "all" applies no filter.'
      ),
  },
  async ({ agent, limit, unread_only, filter }) => {
    const target = agent || AGENT_ID;
    const params = new URLSearchParams({
      limit: String(limit),
      unread_only: String(unread_only),
    });
    if (filter) params.set("filter", filter);
    const url = `${DISPATCHER}/mesh/inbox/${encodeURIComponent(target)}?${params}`;
    try {
      const r = await fetch(url);
      const data = await r.json();
      if (!r.ok) {
        return {
          content: [
            { type: "text", text: `Dispatcher error (HTTP ${r.status}): ${JSON.stringify(data)}` },
          ],
        };
      }
      const lines = data.map(
        (m) =>
          `#${m.id} ${m.from}>${m.to} state=${m.state} ` +
          `reply_to=${m.reply_to ?? "null"} subject=${JSON.stringify(m.subject)}`
      );
      const label = filter || (unread_only ? "unreplied" : "all");
      // `data.length` is the PAGE SIZE, and printing it as the count is how a cap
      // gets read as a measurement (Opie #2339). Prefer the dispatcher's real
      // total; when an older dispatcher does not send one, say "at least" rather
      // than quietly asserting a number we cannot know.
      const total = Number(r.headers.get("x-inbox-total"));
      const truncated = r.headers.get("x-inbox-truncated") === "true";
      let count;
      if (Number.isFinite(total) && r.headers.get("x-inbox-total") !== null) {
        count = truncated
          ? `${data.length} of ${total} message(s) — TRUNCATED, ${total - data.length} not shown (raise limit)`
          : `${total} message(s)`;
      } else {
        count =
          data.length === limit
            ? `at least ${data.length} message(s) — page is FULL, total unknown (dispatcher predates X-Inbox-Total)`
            : `${data.length} message(s)`;
      }
      const header = `Inbox for ${target} (${label}) — ${count}:`;
      return {
        content: [
          {
            type: "text",
            text: lines.length ? `${header}\n${lines.join("\n")}` : `${header}\n(empty)`,
          },
        ],
      };
    } catch (e) {
      return {
        content: [{ type: "text", text: `Dispatcher unreachable at ${DISPATCHER}: ${e.message}` }],
      };
    }
  }
);

// --- thread ---
server.tool(
  "thread",
  `Fetch a complete reply thread by message id. Walks back to the root and ` +
    `returns every message in the conversation in chronological order. ` +
    `Useful for catching up on context before responding.`,
  {
    id: z
      .number()
      .int()
      .min(1)
      .describe("Any message id in the thread. The root is computed automatically."),
  },
  async ({ id }) => {
    try {
      const r = await fetch(`${DISPATCHER}/mesh/thread/${id}`);
      const data = await r.json();
      if (!r.ok) {
        return {
          content: [
            { type: "text", text: `Dispatcher error (HTTP ${r.status}): ${JSON.stringify(data)}` },
          ],
        };
      }
      return {
        content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
      };
    } catch (e) {
      return {
        content: [{ type: "text", text: `Dispatcher unreachable at ${DISPATCHER}: ${e.message}` }],
      };
    }
  }
);

await server.connect(new StdioServerTransport());
