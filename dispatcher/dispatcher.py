#!/usr/bin/env python3
"""
Disco-Bus Dispatcher

HTTP service that accepts envelopes via /mesh/ping and pushes them to per-agent
listeners. One uniform delivery model: every agent runs a tiny HTTP listener,
the dispatcher POSTs envelopes directly, agents wake instantly.

State machine: SENT -> DELIVERED | FAILED.

Configuration:
  Listens on 127.0.0.1:9100 (override via DISCOBUS_HOST / DISCOBUS_PORT).
  SQLite state at ~/.disco-bus/disco-bus.sqlite (override via DISCOBUS_DB).
  Agent registry at ~/.disco-bus/agents.json (override via DISCOBUS_AGENTS_FILE).

Routes:
  POST /mesh/ping        accept envelope, return {id, tracking_id, state}
  GET  /mesh/state/{id}  single envelope by id (full body)
  GET  /mesh/history     recent envelopes (summary list)
  GET  /healthz          liveness
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
    delivery_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_agent, state);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages(reply_to);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
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
    }


class DispatcherHandler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/mesh/ping":
            return self._json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            return self._json(400, {"error": f"invalid JSON: {e}"})

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
        cur = conn.execute(
            """INSERT INTO messages
               (mesh_version, tracking_id, from_agent, to_agent, reply_to,
                subject, body, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'SENT', ?)""",
            (
                MESH_VERSION,
                "pending",
                data["from"],
                data["to"],
                data.get("reply_to"),
                data["subject"],
                body_json,
                created_at,
            ),
        )
        msg_id = cur.lastrowid
        assert msg_id is not None
        tracking_id = make_tracking_id(msg_id, created_at)
        conn.execute("UPDATE messages SET tracking_id=? WHERE id=?", (tracking_id, msg_id))
        conn.commit()
        conn.close()

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
