#!/usr/bin/env python3
"""
Disco-Bus generic listener

One process per agent. Receives envelopes via POST /inbox and forwards
them to the agent's runtime.

Configured via env:
  DISCOBUS_AGENT       agent name (must exist in the agents registry; required)
  DISCOBUS_PORT        port to bind on 127.0.0.1 (required)
  DISCOBUS_DISPATCHER  dispatcher base URL (default: http://127.0.0.1:9100)
  DISCOBUS_AGENTS_FILE path to agents registry (default ~/.disco-bus/agents.json)
  DISCOBUS_INBOX       inbox root (default ~/.disco-bus/inbox)
  DISCOBUS_AUTO_REPLY  optional. Path to an executable that will be invoked
                       with the envelope JSON on stdin. The executable's
                       stdout becomes the reply body (parsed as JSON if
                       possible, else wrapped as {"text": ...}). Reply is
                       posted back to the dispatcher with reply_to set.
                       Skipped if the incoming envelope already has reply_to
                       (avoids auto-reply loops).
  DISCOBUS_AUTO_REPLY_TIMEOUT  seconds (default 120)

Behavior:
  - Always: write envelope to <inbox>/<agent>/<tracking_id>.json (atomic)
  - Optional: if DISCOBUS_AUTO_REPLY is set, invoke it and post the reply
  - Returns 200 immediately; processing happens in a background daemon thread.

Example auto-reply executable (bash):
  #!/usr/bin/env bash
  read -r envelope_json
  echo "{\"ack\":true,\"note\":\"received and acknowledged\"}"
"""

import json
import logging
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

AGENT = os.environ.get("DISCOBUS_AGENT", "")
PORT = int(os.environ.get("DISCOBUS_PORT", "0"))
DISPATCHER = os.environ.get("DISCOBUS_DISPATCHER", "http://127.0.0.1:9100")
AGENTS_FILE = Path(
    os.environ.get("DISCOBUS_AGENTS_FILE", str(Path.home() / ".disco-bus" / "agents.json"))
)
INBOX_ROOT = Path(
    os.environ.get("DISCOBUS_INBOX", str(Path.home() / ".disco-bus" / "inbox"))
)
AUTO_REPLY_CMD = os.environ.get("DISCOBUS_AUTO_REPLY", "").strip() or None
AUTO_REPLY_TIMEOUT = int(os.environ.get("DISCOBUS_AUTO_REPLY_TIMEOUT", "120"))

MESH_VERSION = "0.5"
HOST = "127.0.0.1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [listener-{}] %(message)s".format(AGENT or "?"))
log = logging.getLogger(f"listener-{AGENT}")


def load_valid_agents() -> set[str]:
    if not AGENTS_FILE.exists():
        return set()
    try:
        with open(AGENTS_FILE) as f:
            return set(json.load(f).keys())
    except (OSError, json.JSONDecodeError):
        return set()


def write_inbox(envelope: dict) -> Path:
    inbox = INBOX_ROOT / AGENT
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / f"{envelope['tracking_id']}.json"
    tmp = target.with_suffix(f".json.tmp.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(envelope, indent=2))
    tmp.rename(target)
    return target


def run_auto_reply(envelope: dict) -> dict:
    """Invoke DISCOBUS_AUTO_REPLY with envelope JSON on stdin, parse stdout."""
    assert AUTO_REPLY_CMD is not None
    try:
        result = subprocess.run(
            [AUTO_REPLY_CMD],
            input=json.dumps(envelope),
            capture_output=True,
            text=True,
            timeout=AUTO_REPLY_TIMEOUT,
        )
        text = result.stdout.strip()
        if not text:
            return {"error": "auto-reply returned empty", "stderr": result.stderr.strip()[:300]}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text[:2000]}
    except subprocess.TimeoutExpired:
        return {"error": f"auto-reply timeout after {AUTO_REPLY_TIMEOUT}s"}
    except FileNotFoundError:
        return {"error": f"auto-reply command not found: {AUTO_REPLY_CMD}"}
    except Exception as e:
        return {"error": str(e)[:500]}


def post_reply(envelope: dict, reply_body: dict) -> None:
    payload = {
        "mesh_version": MESH_VERSION,
        "from": AGENT,
        "to": envelope["from"],
        "reply_to": envelope["id"],
        "subject": f"re: {envelope['subject']}",
        "body": reply_body,
    }
    try:
        r = requests.post(f"{DISPATCHER}/mesh/ping", json=payload, timeout=10)
        log.info(f"reply for #{envelope['id']} -> dispatcher {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        log.error(f"reply post failed: {e}")


def handle(envelope: dict) -> None:
    msg_id = envelope.get("id", "?")
    sender = envelope.get("from", "?")
    subj = envelope.get("subject", "")
    log.info(f"received #{msg_id} from {sender} subj={subj!r}")

    try:
        path = write_inbox(envelope)
        log.info(f"wrote inbox: {path}")
    except Exception as e:
        log.error(f"inbox write failed: {e}")
        return

    # Optional auto-reply: skip if envelope is itself a reply (avoid loops)
    if AUTO_REPLY_CMD and envelope.get("reply_to") is None:
        reply = run_auto_reply(envelope)
        post_reply(envelope, reply)


class InboxHandler(BaseHTTPRequestHandler):
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
        if self.path == "/healthz":
            return self._json(200, {"status": "ok", "agent": AGENT, "mesh_version": MESH_VERSION})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/inbox":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            envelope = json.loads(self.rfile.read(n))
        except json.JSONDecodeError as e:
            return self._json(400, {"error": f"invalid JSON: {e}"})
        if not isinstance(envelope, dict):
            return self._json(400, {"error": "envelope must be an object"})
        if envelope.get("mesh_version") != MESH_VERSION:
            return self._json(
                400, {"error": f"mesh_version must be '{MESH_VERSION}', got {envelope.get('mesh_version')!r}"}
            )
        if envelope.get("to") != AGENT:
            return self._json(400, {"error": f"envelope to={envelope.get('to')!r} != listener AGENT={AGENT}"})
        threading.Thread(target=handle, args=(envelope,), daemon=True).start()
        return self._json(200, {"state": "DELIVERED", "agent": AGENT})


def main():
    valid = load_valid_agents()
    if not AGENT:
        raise SystemExit("DISCOBUS_AGENT must be set")
    if valid and AGENT not in valid:
        raise SystemExit(f"DISCOBUS_AGENT={AGENT!r} not in registry {AGENTS_FILE}: {sorted(valid)}")
    if not (1 <= PORT <= 65535):
        raise SystemExit(f"DISCOBUS_PORT must be 1..65535, got {PORT}")
    INBOX_ROOT.mkdir(parents=True, exist_ok=True)
    log.info(f"starting on {HOST}:{PORT} for agent {AGENT}")
    log.info(f"inbox: {INBOX_ROOT / AGENT}")
    log.info(f"dispatcher: {DISPATCHER}")
    if AUTO_REPLY_CMD:
        log.info(f"auto-reply enabled: {AUTO_REPLY_CMD} (timeout {AUTO_REPLY_TIMEOUT}s)")
    else:
        log.info("auto-reply disabled (inbox-only mode)")
    ThreadingHTTPServer((HOST, PORT), InboxHandler).serve_forever()


if __name__ == "__main__":
    main()
