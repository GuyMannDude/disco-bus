"""v0.21: after DISCOBUS_AUTO_REPLY answers a letter and the dispatcher takes
the reply, the listener leaves <tracking_id>.replied.json beside the letter
(snag-rocky-double-ack-one-letter). No sidecar for an error body or a refused
post: those are not answers, so the agent's own reply must still go through.
Runs the real listener against a stub dispatcher."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
LISTENER = ROOT / "listeners" / "listener.py"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="auto-reply fixture is a bash script")


class _Dispatcher(BaseHTTPRequestHandler):
    received: list = []
    status = 202

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(body)
        out = json.dumps({"id": 900 + len(type(self).received), "state": "SENT"}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def rig(tmp_path):
    _Dispatcher.received = []
    _Dispatcher.status = 202
    httpd = HTTPServer(("127.0.0.1", 0), _Dispatcher)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    reply = tmp_path / "reply.json"
    reply.write_text('{"text": "on it"}')
    wake = tmp_path / "wake.sh"
    wake.write_text(f"#!/usr/bin/env bash\ncat >/dev/null\ncat {reply}\n")
    wake.chmod(0o755)
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"alpha": {}}))
    port = _free_port()
    env = {**os.environ,
           "DISCOBUS_AGENT": "alpha", "DISCOBUS_PORT": str(port),
           "DISCOBUS_AGENTS_FILE": str(agents), "DISCOBUS_INBOX": str(tmp_path / "inbox"),
           "DISCOBUS_DISPATCHER": f"http://127.0.0.1:{httpd.server_port}",
           "DISCOBUS_AUTO_REPLY": str(wake)}
    env.pop("DISCOBUS_ON_DELIVER", None)
    proc = subprocess.Popen([sys.executable, str(LISTENER)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                if requests.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5).ok:
                    break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            raise RuntimeError("listener did not come up")
        yield port, tmp_path / "inbox" / "alpha", reply
    finally:
        proc.kill()
        proc.wait(timeout=5)
        httpd.shutdown()
        httpd.server_close()


def _send(port, msg_id, **extra):
    env = {"mesh_version": "0.5", "id": msg_id, "tracking_id": f"msg-{msg_id}-20261105T210804",
           "from": "beta", "to": "alpha", "subject": "probe", "body": {"text": "hi"}, **extra}
    assert requests.post(f"http://127.0.0.1:{port}/inbox", json=env, timeout=5).ok


def _wait(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_answered_letter_gets_a_sidecar_naming_the_reply(rig):
    port, inbox, _ = rig
    _send(port, 6020)
    side = inbox / "msg-6020-20261105T210804.replied.json"
    assert _wait(side.exists)
    s = json.loads(side.read_text())
    assert s["letter_id"] == 6020 and s["reply_id"] == 901 and s["to"] == "beta"
    assert s["replied_at"].endswith("+00:00")
    assert [r["reply_to"] for r in _Dispatcher.received] == [6020]
    assert not list(inbox.glob("*.tmp.*"))      # atomic write left no debris


def test_error_body_is_posted_but_leaves_no_sidecar(rig):
    port, inbox, reply = rig
    reply.write_text('{"error": "hud create session: refused"}')
    _send(port, 6021)
    assert _wait(lambda: len(_Dispatcher.received) == 1)
    time.sleep(0.3)
    assert not list(inbox.glob("*.replied.json"))


def test_refused_reply_post_leaves_no_sidecar(rig):
    port, inbox, _ = rig
    _Dispatcher.status = 500
    _send(port, 6022)
    assert _wait(lambda: len(_Dispatcher.received) == 1)
    time.sleep(0.3)
    assert not list(inbox.glob("*.replied.json"))


def test_a_reply_letter_is_not_auto_answered_and_gets_no_sidecar(rig):
    port, inbox, _ = rig
    _send(port, 6023, reply_to=6000)
    assert _wait((inbox / "msg-6023-20261105T210804.json").exists)
    time.sleep(0.3)
    assert _Dispatcher.received == [] and not list(inbox.glob("*.replied.json"))
