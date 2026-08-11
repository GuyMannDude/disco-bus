#!/usr/bin/env python3
"""
Disco-Bus Dispatcher

HTTP service that accepts envelopes via /mesh/ping and pushes them to per-agent
listeners. One uniform delivery model: every agent runs a tiny HTTP listener,
the dispatcher POSTs envelopes directly, agents wake instantly.

State machine: SENT -> DELIVERED | FAILED.
Paused sender:  HELD -> SENT (play / release) | DROPPED (drop).

Pause (IRIS phase 2, spec bus-lamp-pause-spec.md): a paused agent's pings land
as HELD and are not delivered — enforced here, not by agent promises. HELD and
DROPPED rows never reach listeners and are excluded from inbox views; they are
visible via /mesh/held, /mesh/pause, and the debug surfaces (history/state/
thread). mesh_version stays 0.5: the wire contract (ping input, delivered
envelope) is unchanged — a flushed message is delivered as a normal SENT.

Configuration:
  Listens on 127.0.0.1:9100 (override via DISCOBUS_HOST / DISCOBUS_PORT).
  SQLite state at ~/.disco-bus/disco-bus.sqlite (override via DISCOBUS_DB).
  Agent registry at ~/.disco-bus/agents.json (override via DISCOBUS_AGENTS_FILE).

Routes:
  POST /mesh/ping         accept envelope, return {id, tracking_id, state}
  POST /mesh/pause        {agent} — hold that agent's future pings
  POST /mesh/play         {agent} — lift pause, flush its HELD in id order
  POST /mesh/release/{id} {agent} — send ONE held message (sender only)
  POST /mesh/drop/{id}    {agent} — HELD -> DROPPED, never delivered (sender only)
  GET  /mesh/pause        pause state + held counts, all agents
  GET  /mesh/held[/{agent}] held envelopes (oldest first)
  GET  /mesh/state/{id}   single envelope by id (full body)
  GET  /mesh/history      recent envelopes (summary list)
  GET  /healthz           liveness
"""

import json
import logging
import os
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

import discord_mirror

MESH_VERSION = "0.5"
HOST = os.environ.get("DISCOBUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("DISCOBUS_PORT", "9100"))
DB_PATH = Path(os.environ.get("DISCOBUS_DB", str(Path.home() / ".disco-bus" / "disco-bus.sqlite")))
AGENTS_PATH = Path(os.environ.get("DISCOBUS_AGENTS_FILE", str(Path.home() / ".disco-bus" / "agents.json")))
DELIVER_TIMEOUT_SEC = 10
HISTORY_LIMIT_DEFAULT = 50
HISTORY_LIMIT_MAX = 500
INBOX_LIMIT_DEFAULT = 50
INBOX_LIMIT_MAX = 500
# Cap on the JSON-encoded body. Default 1 MiB. Override via env if you have a
# specific reason to allow larger payloads.
MAX_BODY_BYTES = int(os.environ.get("DISCOBUS_MAX_BODY_BYTES", str(1024 * 1024)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [disco-bus] %(message)s")
log = logging.getLogger("disco-bus")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mesh_version TEXT NOT NULL,
    tracking_id TEXT NOT NULL UNIQUE,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    reply_to INTEGER REFERENCES messages(id),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'SENT',
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    delivery_error TEXT,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_agent, state);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages(reply_to);
CREATE TABLE IF NOT EXISTS pauses (
    agent TEXT PRIMARY KEY,
    paused_at TEXT NOT NULL
);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # Existing messages intentionally remain unread after this migration.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "read_at" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN read_at TEXT")
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_agents() -> dict:
    """Read the agent registry. Single source of truth for which agents exist."""
    if not AGENTS_PATH.exists():
        return {}
    with open(AGENTS_PATH) as f:
        return json.load(f)


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def make_tracking_id(msg_id: int, created_at: str) -> str:
    ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return f"msg-{msg_id}-{ts.strftime('%Y%m%dT%H%M%S')}"


def update_state(msg_id: int, state: str, error: str | None = None) -> None:
    conn = get_db()
    if state == "DELIVERED":
        conn.execute(
            "UPDATE messages SET state=?, delivered_at=? WHERE id=?",
            (state, now_utc(), msg_id),
        )
    else:
        conn.execute(
            "UPDATE messages SET state=?, delivery_error=? WHERE id=?",
            (state, error, msg_id),
        )
    conn.commit()
    conn.close()
    suffix = f" ({error})" if error else ""
    log.info(f"#{msg_id} -> {state}{suffix}")


def deliver(envelope: dict, agents: dict) -> None:
    """Background thread. POST envelope to target listener; update state in DB."""
    msg_id = envelope["id"]
    target = envelope["to"]
    cfg = agents.get(target)
    if not cfg or "url" not in cfg:
        update_state(msg_id, "FAILED", error=f"no listener registered for {target}")
        return
    try:
        r = requests.post(cfg["url"], json=envelope, timeout=DELIVER_TIMEOUT_SEC)
    except requests.RequestException as e:
        update_state(msg_id, "FAILED", error=f"transport: {e}"[:500])
        return
    if r.status_code == 200:
        update_state(msg_id, "DELIVERED")
        discord_mirror.mirror(envelope)
    else:
        update_state(msg_id, "FAILED", error=f"listener {r.status_code}: {r.text[:300]}")


def flush_held(msg_ids: list[int], agents: dict) -> None:
    """Background thread. Deliver formerly-HELD messages ONE AT A TIME, in the
    order given (id ASC) — a thread per message would race away the ordering
    the spec promises ("play flushes in order"). Rows were already flipped to
    SENT by the caller under rowcount guard, so a concurrent second play
    cannot double-deliver."""
    for msg_id in msg_ids:
        conn = get_db()
        row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        conn.close()
        if row is None:
            continue
        deliver(row_to_envelope(row), agents)


def held_counts() -> dict[str, int]:
    """HELD rows per sender."""
    conn = get_db()
    rows = conn.execute(
        "SELECT from_agent, COUNT(*) AS n FROM messages WHERE state='HELD' GROUP BY from_agent"
    ).fetchall()
    conn.close()
    return {r["from_agent"]: r["n"] for r in rows}


def validate_envelope_input(data, valid_agents: set) -> tuple[bool, str | None]:
    """Validate /mesh/ping body. Returns (ok, error_msg)."""
    if not isinstance(data, dict):
        return False, "body must be a JSON object"
    if data.get("mesh_version") != MESH_VERSION:
        return False, f"mesh_version must be '{MESH_VERSION}', got {data.get('mesh_version')!r}"
    for field in ("from", "to", "subject", "body"):
        if field not in data:
            return False, f"missing required field: {field}"
    if data["from"] not in valid_agents:
        return False, f"unknown 'from' agent: {data['from']} (registry: {sorted(valid_agents)})"
    if data["to"] not in valid_agents:
        return False, f"unknown 'to' agent: {data['to']} (registry: {sorted(valid_agents)})"
    if not isinstance(data["subject"], str) or not data["subject"]:
        return False, "subject must be a non-empty string"
    if not isinstance(data["body"], dict):
        return False, "body must be an object"
    rt = data.get("reply_to")
    if rt is not None and not (isinstance(rt, int) and rt >= 1):
        return False, "reply_to must be a positive integer or null"
    body_bytes = len(json.dumps(data["body"]).encode("utf-8"))
    if body_bytes > MAX_BODY_BYTES:
        return False, f"body too large: {body_bytes} bytes > limit {MAX_BODY_BYTES}"
    return True, None


def row_to_envelope(row) -> dict:
    return {
        "mesh_version": row["mesh_version"],
        "id": row["id"],
        "tracking_id": row["tracking_id"],
        "from": row["from_agent"],
        "to": row["to_agent"],
        "reply_to": row["reply_to"],
        "subject": row["subject"],
        "body": json.loads(row["body"]),
        "state": row["state"],
        "created_at": row["created_at"],
        "delivered_at": row["delivered_at"],
        "delivery_error": row["delivery_error"],
        "read_at": row["read_at"],
    }


class DispatcherHandler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        log.info(f"{self.address_string()} {format % args}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            return self._json(200, {"status": "ok", "mesh_version": MESH_VERSION})

        if path.startswith("/mesh/state/"):
            try:
                msg_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._json(400, {"error": "invalid id"})
            conn = get_db()
            row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            conn.close()
            if not row:
                return self._json(404, {"error": "not found"})
            return self._json(200, row_to_envelope(row))

        if path == "/mesh/history":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", [HISTORY_LIMIT_DEFAULT])[0])
            except ValueError:
                return self._json(400, {"error": "invalid limit"})
            limit = max(1, min(limit, HISTORY_LIMIT_MAX))
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
            return self._json(200, [row_to_envelope(r) for r in rows])

        # /mesh/inbox/<agent> — list messages addressed to that agent (newest first).
        # Optional query: ?limit=N&filter=unread|unreplied|all.
        # Legacy ?unread_only=true remains an alias for filter=unreplied.
        if path.startswith("/mesh/inbox/"):
            agent = path[len("/mesh/inbox/"):]
            if not agent or "/" in agent:
                return self._json(400, {"error": "invalid agent in path"})
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", [INBOX_LIMIT_DEFAULT])[0])
            except ValueError:
                return self._json(400, {"error": "invalid limit"})
            limit = max(1, min(limit, INBOX_LIMIT_MAX))
            unread_only = qs.get("unread_only", ["false"])[0].lower() in ("true", "1", "yes")
            inbox_filter = qs.get("filter", [None])[0]
            if inbox_filter is None:
                inbox_filter = "unreplied" if unread_only else "all"
            if inbox_filter not in ("unread", "unreplied", "all"):
                return self._json(
                    400, {"error": "filter must be one of: unread, unreplied, all"}
                )
            # Inbox never shows HELD/DROPPED: an undelivered draft is not mail,
            # and a held one showing up unread would leak exactly what pause
            # exists to hold back. A HELD reply likewise does not clear
            # unreplied — the recipient has not been answered until it ships.
            # The page and the total MUST come from one predicate. Two copies of
            # this WHERE clause is how a count starts disagreeing with the rows it
            # claims to count -- and a listing that miscounts is worse than one
            # that does not count at all (Opie #2339: a capped page read as a
            # total, reported to Guy as a measurement for three days).
            if inbox_filter == "unread":
                from_where = """FROM messages m
                       WHERE m.to_agent = ? AND m.read_at IS NULL
                         AND m.state NOT IN ('HELD','DROPPED')"""
                params = (agent,)
            elif inbox_filter == "unreplied":
                # Messages addressed to <agent> that have no reply *from* that
                # agent. A "reply" is any row whose reply_to == m.id and
                # from_agent == <agent>.
                from_where = """FROM messages m
                       WHERE m.to_agent = ?
                         AND m.state NOT IN ('HELD','DROPPED')
                         AND NOT EXISTS (
                           SELECT 1 FROM messages r
                           WHERE r.reply_to = m.id
                             AND r.from_agent = ?
                             AND r.state NOT IN ('HELD','DROPPED')
                         )"""
                params = (agent, agent)
            else:
                from_where = """FROM messages m
                       WHERE m.to_agent = ?
                         AND m.state NOT IN ('HELD','DROPPED')"""
                params = (agent,)

            conn = get_db()
            rows = conn.execute(
                f"SELECT m.* {from_where} ORDER BY m.id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            # COUNT over the same predicate, WITHOUT the limit. This is the whole
            # point: `len(rows)` can only ever report the page size.
            total = conn.execute(f"SELECT COUNT(*) {from_where}", params).fetchone()[0]
            conn.close()
            # Carried as headers, not body fields: the body stays a bare array so
            # every existing consumer (listeners, tests, the MCP bridge) is
            # untouched, and the comment at /mesh/read still holds -- these
            # describe the call, not the rows.
            return self._json(
                200,
                [row_to_envelope(r) for r in rows],
                headers={
                    "X-Inbox-Total": total,
                    "X-Inbox-Returned": len(rows),
                    "X-Inbox-Truncated": "true" if total > len(rows) else "false",
                },
            )

        # /mesh/pause — pause state + held counts for every agent. "Orphaned"
        # held rows (sender no longer paused — a crash window or a drop that
        # never happened) are reported, not hidden: a held draft nobody is
        # watching is the loss doctrine-loss-invisible warns about.
        if path == "/mesh/pause":
            conn = get_db()
            prows = conn.execute("SELECT agent, paused_at FROM pauses ORDER BY agent").fetchall()
            conn.close()
            held = held_counts()
            paused = [
                {"agent": r["agent"], "paused_at": r["paused_at"],
                 "held": held.get(r["agent"], 0)}
                for r in prows
            ]
            paused_names = {p["agent"] for p in paused}
            orphans = [
                {"agent": a, "held": n}
                for a, n in sorted(held.items()) if a not in paused_names
            ]
            return self._json(200, {"paused": paused, "held_orphans": orphans})

        # /mesh/held or /mesh/held/<agent> — held envelopes, oldest first
        # (the order play will flush them). Any agent may ask ("what's held?"
        # is answerable on request, spec guardrail 4).
        if path == "/mesh/held" or path.startswith("/mesh/held/"):
            agent = path[len("/mesh/held/"):] if path.startswith("/mesh/held/") else ""
            if "/" in agent:
                return self._json(400, {"error": "invalid agent in path"})
            conn = get_db()
            if agent:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE state='HELD' AND from_agent=? ORDER BY id ASC",
                    (agent,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE state='HELD' ORDER BY id ASC"
                ).fetchall()
            conn.close()
            return self._json(200, [row_to_envelope(r) for r in rows])

        # /mesh/thread/<id> — full reply chain. Walks reply_to back to the root,
        # then returns every message in that thread in chronological order.
        if path.startswith("/mesh/thread/"):
            try:
                msg_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._json(400, {"error": "invalid id"})
            conn = get_db()
            # Walk back to root via reply_to
            cur_id: int | None = msg_id
            seen: set[int] = set()
            root_id = msg_id
            while cur_id is not None and cur_id not in seen:
                seen.add(cur_id)
                row = conn.execute(
                    "SELECT reply_to FROM messages WHERE id = ?", (cur_id,)
                ).fetchone()
                if row is None:
                    conn.close()
                    return self._json(404, {"error": f"message {cur_id} not found"})
                parent = row["reply_to"]
                if parent is None:
                    root_id = cur_id
                    break
                root_id = parent
                cur_id = parent
            # Recursive CTE to collect the whole thread under root_id
            rows = conn.execute(
                """WITH RECURSIVE thread(id) AS (
                       SELECT id FROM messages WHERE id = ?
                       UNION ALL
                       SELECT m.id FROM messages m
                       JOIN thread t ON m.reply_to = t.id
                   )
                   SELECT * FROM messages WHERE id IN thread
                   ORDER BY id ASC""",
                (root_id,),
            ).fetchall()
            conn.close()
            return self._json(200, {
                "root_id": root_id,
                "messages": [row_to_envelope(r) for r in rows],
            })

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            return self._json(400, {"error": f"invalid JSON: {e}"})

        # Only ping_read uses this recipient-bound mutation. Listing inbox,
        # history, threads, or raw state never marks a message read.
        if path.startswith("/mesh/read/"):
            try:
                msg_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._json(400, {"error": "invalid id"})
            if not isinstance(data, dict) or not isinstance(data.get("agent"), str):
                return self._json(400, {"error": "agent is required"})
            conn = get_db()
            row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            if not row:
                conn.close()
                return self._json(404, {"error": "not found"})
            if row["to_agent"] != data["agent"]:
                conn.close()
                return self._json(403, {"error": "only the recipient can mark this message read"})
            # first_read tells the caller whether THIS call opened the message.
            # Without it the response is identical either way, and a reader that
            # sees a populated read_at cannot tell the timestamp its own call just
            # wrote from one an earlier read left behind. Derived from rowcount,
            # not the check above, so a losing racer is correctly told "not first".
            first_read = False
            if row["read_at"] is None:
                cursor = conn.execute(
                    "UPDATE messages SET read_at=? WHERE id=? AND read_at IS NULL",
                    (now_utc(), msg_id),
                )
                conn.commit()
                first_read = cursor.rowcount == 1
                row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            conn.close()
            # Not part of row_to_envelope: it describes this call, not the row, so
            # inbox/history/state listings must not grow a meaningless field.
            return self._json(200, {**row_to_envelope(row), "first_read": first_read})

        # /mesh/pause — hold every future ping FROM this agent. Idempotent;
        # re-pausing keeps the original paused_at (the hold started then).
        if path == "/mesh/pause":
            agent = data.get("agent") if isinstance(data, dict) else None
            if not isinstance(agent, str) or not agent:
                return self._json(400, {"error": "agent is required"})
            if agent not in load_agents():
                return self._json(400, {"error": f"unknown agent: {agent}"})
            conn = get_db()
            conn.execute(
                "INSERT OR IGNORE INTO pauses (agent, paused_at) VALUES (?, ?)",
                (agent, now_utc()),
            )
            conn.commit()
            row = conn.execute("SELECT paused_at FROM pauses WHERE agent=?", (agent,)).fetchone()
            conn.close()
            log.info(f"pause {agent}")
            return self._json(200, {
                "agent": agent, "paused": True, "paused_at": row["paused_at"],
                "held": held_counts().get(agent, 0),
            })

        # /mesh/play — lift the pause and flush that agent's HELD in id order.
        # Rows flip to SENT here under a rowcount guard, so a racing second
        # play (or release) can never double-deliver; actual delivery runs in
        # one background thread to keep the promised ordering.
        if path == "/mesh/play":
            agent = data.get("agent") if isinstance(data, dict) else None
            if not isinstance(agent, str) or not agent:
                return self._json(400, {"error": "agent is required"})
            if agent not in load_agents():
                return self._json(400, {"error": f"unknown agent: {agent}"})
            conn = get_db()
            # One IMMEDIATE transaction across delete + collect + mark: pairs
            # with the BEGIN IMMEDIATE in /mesh/ping so a racing ping either
            # lands before the SELECT (and flushes now) or after the commit
            # (and sends normally) — never invisibly between (review #2
            # finding 10).
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM pauses WHERE agent=?", (agent,))
            held_rows = conn.execute(
                "SELECT * FROM messages WHERE state='HELD' AND from_agent=? ORDER BY id ASC",
                (agent,),
            ).fetchall()
            released = []
            for r in held_rows:
                cur = conn.execute(
                    "UPDATE messages SET state='SENT' WHERE id=? AND state='HELD'",
                    (r["id"],),
                )
                if cur.rowcount == 1:
                    released.append({"id": r["id"], "tracking_id": r["tracking_id"],
                                     "to": r["to_agent"], "subject": r["subject"]})
            conn.commit()
            conn.close()
            log.info(f"play {agent}: releasing {len(released)} held")
            if released:
                agents = load_agents()
                ids = [m["id"] for m in released]
                threading.Thread(target=flush_held, args=(ids, agents), daemon=True).start()
            return self._json(200, {"agent": agent, "paused": False, "released": released})

        # /mesh/release/<id> — send ONE held message while still paused. The
        # open path for a genuinely hot item (spec guardrail 2: announced in
        # chat, done in the open — never a hidden bypass). Sender only.
        # /mesh/drop/<id> — HELD -> DROPPED, never delivered. Sender only.
        # The row stays in the DB: history is load-bearing, dropped != erased.
        if path.startswith("/mesh/release/") or path.startswith("/mesh/drop/"):
            action = "release" if path.startswith("/mesh/release/") else "drop"
            try:
                msg_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._json(400, {"error": "invalid id"})
            if not isinstance(data, dict) or not isinstance(data.get("agent"), str):
                return self._json(400, {"error": "agent is required"})
            conn = get_db()
            row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            if not row:
                conn.close()
                return self._json(404, {"error": "not found"})
            if row["from_agent"] != data["agent"]:
                conn.close()
                return self._json(403, {"error": f"only the sender can {action} a held message"})
            new_state = "SENT" if action == "release" else "DROPPED"
            cur = conn.execute(
                "UPDATE messages SET state=? WHERE id=? AND state='HELD'",
                (new_state, msg_id),
            )
            conn.commit()
            if cur.rowcount != 1:
                conn.close()
                return self._json(409, {"error": f"message is not HELD (state={row['state']})"})
            row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            conn.close()
            log.info(f"{action} #{msg_id} by {data['agent']}")
            if action == "release":
                threading.Thread(
                    target=flush_held, args=([msg_id], load_agents()), daemon=True
                ).start()
            return self._json(200, row_to_envelope(row))

        if path != "/mesh/ping":
            return self._json(404, {"error": "not found"})

        agents = load_agents()
        valid_agents = set(agents.keys())
        if not valid_agents:
            return self._json(503, {"error": f"agents registry empty at {AGENTS_PATH}"})

        ok, err = validate_envelope_input(data, valid_agents)
        if not ok:
            return self._json(400, {"error": err})

        created_at = now_utc()
        body_json = json.dumps(data["body"])
        conn = get_db()
        # Pause is enforced HERE. BEGIN IMMEDIATE makes pause-check + insert
        # one atomic unit against /mesh/play's delete-and-flush transaction:
        # without it, a ping racing a play could commit a HELD row after play
        # already collected its flush set — an orphan play just reported as
        # fully flushed (review 2026-08-06 #2 finding 10). The bus holds it,
        # promises don't (spec guardrail: "enforced by the bus, not by
        # promises").
        conn.execute("BEGIN IMMEDIATE")
        paused = conn.execute(
            "SELECT 1 FROM pauses WHERE agent=?", (data["from"],)
        ).fetchone() is not None
        state = "HELD" if paused else "SENT"
        cur = conn.execute(
            """INSERT INTO messages
               (mesh_version, tracking_id, from_agent, to_agent, reply_to,
                subject, body, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                MESH_VERSION,
                "pending",
                data["from"],
                data["to"],
                data.get("reply_to"),
                data["subject"],
                body_json,
                state,
                created_at,
            ),
        )
        msg_id = cur.lastrowid
        assert msg_id is not None
        tracking_id = make_tracking_id(msg_id, created_at)
        conn.execute("UPDATE messages SET tracking_id=? WHERE id=?", (tracking_id, msg_id))
        conn.commit()
        conn.close()

        if paused:
            log.info(f"#{msg_id} HELD ({data['from']} is paused)")
            return self._json(202, {"id": msg_id, "tracking_id": tracking_id, "state": "HELD"})

        envelope = {
            "mesh_version": MESH_VERSION,
            "id": msg_id,
            "tracking_id": tracking_id,
            "from": data["from"],
            "to": data["to"],
            "reply_to": data.get("reply_to"),
            "subject": data["subject"],
            "body": data["body"],
            "state": "SENT",
            "created_at": created_at,
            "delivered_at": None,
            "delivery_error": None,
            "read_at": None,
        }

        threading.Thread(target=deliver, args=(envelope, agents), daemon=True).start()

        return self._json(202, {"id": msg_id, "tracking_id": tracking_id, "state": "SENT"})


def main():
    init_db()
    log.info(f"starting on {HOST}:{PORT}")
    log.info(f"db: {DB_PATH}")
    if AGENTS_PATH.exists():
        log.info(f"agents.json: {AGENTS_PATH} (loaded)")
    else:
        log.info(f"agents.json: {AGENTS_PATH} MISSING — deliveries will FAIL until you register agents")
    server = ThreadingHTTPServer((HOST, PORT), DispatcherHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
