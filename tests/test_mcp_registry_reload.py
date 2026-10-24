"""The MCP `ping` tool checks the agent registry as it is NOW, not as it was
when the server started (snag 2026-10-23: CC4 was registered while CC's
session was running, and that session's ping answered `Unknown agent "CC4"`
until its MCP restarted; the dispatcher had already accepted CC4's letter).

Drives the real mcp/server.js over stdio against a stub dispatcher.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp" / "server.js"
HAVE_NODE = shutil.which("node") is not None and (ROOT / "mcp" / "node_modules").exists()


class _StubDispatcher(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(body)
        out = json.dumps({"id": len(type(self).received), "state": "DELIVERED",
                          "tracking_id": "msg-test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class _Mcp:
    """Newline-delimited JSON-RPC over the server's stdio."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen(["node", str(SERVER)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=env)
        self._id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"server exited: {self.proc.stderr.read()}")
            msg = json.loads(line)
            if msg.get("id") == self._id:
                return msg

    def ping(self, to: str) -> str:
        r = self.request("tools/call", {"name": "ping",
                                        "arguments": {"to": to, "subject": "t", "body": "{}"}})
        return r["result"]["content"][0]["text"]

    def close(self):
        self.proc.stdin.close()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()


@unittest.skipUnless(HAVE_NODE, "node + mcp/node_modules needed")
class RegistryReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agents = Path(self.tmp.name) / "agents.json"
        _StubDispatcher.received = []
        self.httpd = HTTPServer(("127.0.0.1", 0), _StubDispatcher)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
                    "DISCOBUS_AGENT": "A",
                    "DISCOBUS_DISPATCHER": f"http://127.0.0.1:{self.httpd.server_port}",
                    "DISCOBUS_AGENTS_FILE": str(self.agents)}
        self.mcp = None

    def tearDown(self):
        if self.mcp:
            self.mcp.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _write(self, names):
        self.agents.write_text(json.dumps({n: {} for n in names}))

    def test_agent_registered_after_start_is_pingable(self):
        self._write(["A", "B"])
        self.mcp = _Mcp(self.env)
        self.assertIn('Unknown agent "C"', self.mcp.ping("C"))
        self.assertEqual(_StubDispatcher.received, [])
        self._write(["A", "B", "C"])           # registered while the session runs
        self.assertIn("Ping #1 accepted: A>C", self.mcp.ping("C"))
        self.assertEqual([e["to"] for e in _StubDispatcher.received], ["C"])

    def test_unknown_agent_still_refused_and_named_against_the_fresh_list(self):
        self._write(["A", "B"])
        self.mcp = _Mcp(self.env)
        self._write(["A", "B", "C"])
        text = self.mcp.ping("D")
        self.assertIn('Unknown agent "D". Registered: A, B, C', text)
        self.assertEqual(_StubDispatcher.received, [])

    def test_missing_or_empty_registry_accepts_all(self):
        self.mcp = _Mcp(self.env)               # no agents.json at all
        self.assertIn("accepted: A>anyone", self.mcp.ping("anyone"))
        self._write([])                         # {}: nothing registered
        self.assertIn("accepted: A>someone", self.mcp.ping("someone"))
        self.assertEqual([e["to"] for e in _StubDispatcher.received], ["anyone", "someone"])

    def test_registry_removed_after_start_falls_back_to_accept_all(self):
        self._write(["A", "B"])
        self.mcp = _Mcp(self.env)
        self.agents.unlink()
        self.assertIn("accepted: A>Z", self.mcp.ping("Z"))


if __name__ == "__main__":
    unittest.main()
