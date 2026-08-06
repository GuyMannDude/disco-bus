# Architecture

## The three problems Disco-Bus was built to fix

When you have multiple agents (whether they're LLM personas, tool-runners, or just programs), getting them to talk to each other usually breaks down in one of three ways:

1. **Polling** — the receiver walks an inbox every N seconds. Slow, wastes CPU, and N seconds of latency for every message.
2. **Auto-spawn** — a fresh agent process is launched per message. Works, but the agent has no memory of prior conversation. You can ack messages but you can't reason across them.
3. **Pure pub/sub** — every agent must be subscribed and listening. If an agent is offline, the message vanishes.

Disco-Bus is the fourth model: **direct push to a tiny per-agent HTTP listener that always runs.** The listener doesn't *run the agent* — it just writes the envelope to a per-agent inbox directory (and optionally fires an auto-reply hook). The actual agent — whatever it is — reads the inbox on its own schedule.

This separates **transport** from **runtime**. Transport is deterministic, durable, and fast. Runtime can be whatever it needs to be: a stateful chat session that you wake up by typing, a cron job, a long-running daemon, or just `cat ~/.disco-bus/inbox/agent/*.json` when you feel like it.

## The data path

```
  ┌──────────────────────┐
  │   sender agent       │
  │  (via MCP `ping`,    │
  │   or curl, or HTTP)  │
  └──────────┬───────────┘
             │ POST /mesh/ping
             ▼
  ┌──────────────────────┐
  │     Dispatcher       │
  │   :9100              │
  │ ┌──────────────────┐ │
  │ │ INSERT row       │ │  SQLite state — every envelope persisted
  │ │ state=SENT       │ │
  │ └──────────────────┘ │
  └───────┬──────────┬───┘
          │          │ (async, daemon thread)
          ▼          ▼
   ┌──────────┐  ┌────────────────────────────┐
   │ Discord  │  │  recipient listener        │
   │  mirror  │  │  POST /inbox  → :91xx      │
   │ (best-   │  │                            │
   │  effort) │  │  writes ~/.disco-bus/      │
   │          │  │     inbox/<agent>/         │
   │          │  │     <tracking_id>.json     │
   │          │  │                            │
   │          │  │  (optional) auto-reply:    │
   │          │  │     run DISCOBUS_AUTO_     │
   │          │  │     REPLY executable       │
   │          │  │     → post reply via       │
   │          │  │     dispatcher             │
   │          │  └────────────────────────────┘
   │          │                │
   │          │                │ if state=200
   │          │                ▼
   │          │  UPDATE state=DELIVERED
   │          │  (back in dispatcher SQLite)
   │          ▼
   │  (envelope shown in
   │   global firehose +
   │   per-agent channels)
   └─────────────────
```

## Why SQLite

State has to survive process restarts. Every envelope, every state transition, every error string is durable. You can `curl /mesh/state/<id>` an hour after the fact and get the full envelope back. The `mesh/history` endpoint walks the table for debugging.

WAL mode is enabled so concurrent reads (e.g., from a portal UI) don't block writes.

## Why Discord is the *mirror*, not the transport

Three reasons:

1. **Latency.** Discord API is fine for humans (~hundreds of ms) but terrible for agent-to-agent (you want sub-100ms). The bus is direct HTTP.
2. **Loop hazard.** If agents talked *through* Discord, every bot would see every agent's reply, including its own, and you'd need elaborate filters to avoid infinite ack chains. With Discord as a passive *mirror*, this never happens.
3. **Availability.** Discord can rate-limit, go down, or block. The bus shouldn't care. The mirror catches all its exceptions and continues; bus delivery never blocks on Discord.

The mirror is also where you can take this further: post to Slack instead, write to a structured log, fan out to a database, etc. The dispatcher calls `discord_mirror.mirror(envelope)` after delivery succeeds — swap the implementation, keep the seam.

## The auto-reply hook

Listeners default to inbox-only mode (write the file and stop). If you set `DISCOBUS_AUTO_REPLY=<command>`, the listener will spawn that command per inbound message and post the command's stdout as a reply.

This is a useful *trap door* for stateless task-runners. Want a "search agent" that responds instantly? Point `DISCOBUS_AUTO_REPLY` at a shell script that runs the search and outputs JSON. Want an LLM-driven ack? Point it at a script that calls your LLM of choice and parses the response.

**Important nuance:** auto-reply only fires for *new pings* (no `reply_to` on inbound). This prevents two auto-reply agents from infinite-looping at each other.

**Trade-off:** auto-reply uses a fresh subprocess every time. There's no continuity between invocations. If your agent needs to *remember* prior turns, don't use auto-reply — instead, have a stateful process tail the inbox directory and process new files when it's good and ready. The inbox is durable; the agent can sleep, restart, or be offline, and pick up later.

## Why per-agent inboxes are filesystem directories

Two reasons over a queue or in-memory buffer:

1. **Durability for free.** If a listener crashes, the envelopes are still there. Restart and they're available.
2. **Inspectable.** `ls ~/.disco-bus/inbox/<agent>/` is a debugging tool. So is `cat <tracking_id>.json`. No tooling needed.

The cost is filesystem churn. At ten messages per second sustained this would matter; for agent-to-agent communication (handfuls per minute typically) it doesn't.

## What v0.5 explicitly does NOT have

- **Chain tracking.** A `reply_to` field links replies to their prompt, but there's no notion of "this conversation has completed" or "this thread has N turns." You can derive it by walking `reply_to` chains, but the dispatcher doesn't track it.
- **Multi-machine.** Localhost-only. For multi-machine, run a tunnel or fork.
- **Retries.** Listener failure → `state=FAILED` → done. Sender's choice whether to resend.
- **Encryption / auth.** Disco-Bus assumes localhost participants are friendly. The `from` field is server-bound (MCP-level) but the HTTP API has no auth. Don't expose `:9100` to the internet.

These are deliberate omissions. The original problem was "agents don't wake on inbound." Adding chain semantics or retry logic would inflate the codebase past what one person can read end-to-end. v0.5 is intentionally the smallest thing that works.

## The shape of an envelope

Five required fields enter (`mesh_version`, `from`, `to`, `subject`, `body`); the dispatcher fills in `id`, `tracking_id`, `state`, `created_at`. After delivery, `delivered_at` and (on failure) `delivery_error` are populated.

`body` is freeform JSON — the schema deliberately doesn't constrain it. Senders agree on a body shape per-conversation. The dispatcher never inspects body content.

## Read endpoints

Beyond `GET /mesh/state/<id>` and `GET /mesh/history`, the dispatcher offers two convenience queries an agent typically wants when waking up:

- `GET /mesh/inbox/<agent>?limit=N&filter=unread|unreplied|all` — messages addressed to an agent. `unread` is based on nullable `read_at`; `unreplied` is derived from reply chains. Legacy `unread_only=true` remains an alias for `unreplied`.
- `POST /mesh/read/<id>` — recipient-bound read used by `ping_read`. It sets `read_at` only when null and returns the full envelope. Inbox, history, thread, and raw state reads never mark a message read.
- `GET /mesh/thread/<id>` — walks `reply_to` back to the root, then a recursive CTE collects every descendant. Returns `{root_id, messages: [...]}` in chronological order. Useful for catching up on context before responding to anything.

## Pause / play (v0.8)

A human planning with an agent can hold that agent's outbound traffic **in the
dispatcher** — enforcement, not etiquette. `POST /mesh/pause {agent}` inserts a
row in the `pauses` table; from then on every `/mesh/ping` FROM that agent is
persisted as `state=HELD` and never handed to a delivery thread. The sender
keeps refining: send a better version, `POST /mesh/drop/<id>` the stale one
(HELD → DROPPED, kept in history, never delivered), or `POST /mesh/release/<id>`
to ship a single hot item while still paused. `POST /mesh/play {agent}` deletes
the pause row and flushes the agent's HELD messages one at a time in id order —
sequenced in a single thread precisely so "flushes in order" is a property, not
a hope.

HELD and DROPPED are dispatcher-internal states. They are excluded from every
`/mesh/inbox` view (an undelivered draft is not mail, and a HELD reply does not
clear `unreplied`) and never appear on the wire — a flushed message is delivered
as an ordinary SENT envelope, which is why `mesh_version` stays 0.5. They remain
visible in `state`/`history`/`thread` (history is load-bearing) and in the two
dedicated views: `GET /mesh/pause` (pause state + held counts for all agents,
including orphaned held rows whose sender is no longer paused — a crash window
that must stay visible) and `GET /mesh/held[/<agent>]` (held envelopes,
oldest-first).

**Trust model, stated plainly:** pause/play/release/drop ride the bus's
existing no-auth posture — the same one that lets any localhost caller set
`from` on a ping. `release`/`drop` check the claimed sender; `pause`/`play`
check only that the agent exists. So "a paused agent cannot leak" is enforced
against *accidents* (an agent whose tooling pings out of habit), not against
an agent that deliberately POSTs `/mesh/play` at itself — that residual case
is behavioral (spec guardrail 2: bypasses happen in the open, in chat) and
every pause/play/release/drop is logged by the dispatcher for audit. Adding
real caller auth is a bus-wide decision, not a pause-endpoint patch.

The listing/thread endpoints are read-only; only the explicit recipient read endpoint mutates `read_at`. SQLite indexes keep the reply-chain and inbox queries cheap.

## Where to extend

Most useful extension points are also the cleanest seams:

- **`discord_mirror.py`** — swap for Slack, webhook fan-out, structured log, whatever. The dispatcher only knows it called `.mirror(envelope)`.
- **`DISCOBUS_AUTO_REPLY`** — drop in any agent runtime that can read JSON on stdin and emit JSON on stdout.
- **New read endpoints** — the dispatcher's `do_GET` handler is a straightforward router. Add a query like `/mesh/search?q=...` over `subject` or `body` and a matching MCP tool the same way `inbox`/`thread` were added. The SQLite layer is plain `conn.execute` — no ORM in the way.

## Where NOT to extend

Resist the urge to make Disco-Bus itself smarter. Every feature that puts more logic inside the dispatcher pushes you toward a "do everything" message broker, which is the thing this is reacting against. Keep the dispatcher dumb; put intelligence in the agents.
