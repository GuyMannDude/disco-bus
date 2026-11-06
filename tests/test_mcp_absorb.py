"""v0.21: the MCP `ping` tool absorbs a second answer to a letter the
listener's wake path already answered (snag-rocky-double-ack-one-letter:
Rocky's wake reply #6022, then his own `ping` reply_to=6020 as #6024).
Absorb only inside the window, only for the same letter and the same
recipient; anything unreadable fails open. Drives the real mcp/server.js."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path

from test_mcp_registry_reload import HAVE_NODE, _Mcp, _StubDispatcher


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


@unittest.skipUnless(HAVE_NODE, "node + mcp/node_modules needed")
class AbsorbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "agents.json").write_text(json.dumps({"Rocky": {}, "CC": {}, "CC3": {}}))
        self.inbox = root / "inbox" / "Rocky"
        self.inbox.mkdir(parents=True)
        _StubDispatcher.received = []
        self.httpd = HTTPServer(("127.0.0.1", 0), _StubDispatcher)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
               "DISCOBUS_AGENT": "Rocky",
               "DISCOBUS_DISPATCHER": f"http://127.0.0.1:{self.httpd.server_port}",
               "DISCOBUS_AGENTS_FILE": str(root / "agents.json"),
               "DISCOBUS_INBOX": str(root / "inbox")}
        for key in ("SYSTEMROOT", "USERPROFILE"):
            if key in os.environ:
                env[key] = os.environ[key]
        self.mcp = _Mcp(env)

    def tearDown(self):
        self.mcp.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _sidecar(self, letter_id=6020, to="CC", minutes_ago=1.5, name=None, raw=None):
        path = self.inbox / (name or f"msg-{letter_id}-20261105T210804.replied.json")
        path.write_text(raw if raw is not None else json.dumps(
            {"letter_id": letter_id, "reply_id": 6022, "to": to, "replied_at": _iso(minutes_ago)}))

    def _ping(self, to: str = "CC", reply_to: int | None = 6020) -> str:
        args: dict = {"to": to, "subject": "ACK #6020", "body": "ACK"}
        if reply_to is not None:
            args["reply_to"] = reply_to
        r = self.mcp.request("tools/call", {"name": "ping", "arguments": args})
        return r["result"]["content"][0]["text"]

    def test_second_answer_inside_the_window_is_absorbed_and_says_why(self):
        self._sidecar()
        text = self._ping()
        self.assertIn("Absorbed, NOT sent: your wake reply #6022 already answered #6020", text)
        self.assertEqual(_StubDispatcher.received, [])

    def test_reply_after_the_window_goes_through(self):
        self._sidecar(minutes_ago=11)             # an ORDER-COMPLETE later on the thread
        self.assertIn("accepted: Rocky>CC", self._ping())
        self.assertEqual([e["reply_to"] for e in _StubDispatcher.received], [6020])

    def test_other_letter_and_id_prefix_collision_go_through(self):
        self._sidecar(letter_id=602, name="msg-602-20261105T210804.replied.json")
        self.assertIn("accepted", self._ping(reply_to=6020))   # msg-602- is not msg-6020-
        self.assertIn("accepted", self._ping(reply_to=60))     # msg-60- prefix, letter_id 602
        self.assertEqual(len(_StubDispatcher.received), 2)

    def test_reply_to_someone_else_goes_through(self):
        self._sidecar(to="CC")
        self.assertIn("accepted: Rocky>CC3", self._ping(to="CC3"))

    def test_new_letter_without_reply_to_goes_through(self):
        self._sidecar()
        self.assertIn("accepted", self._ping(reply_to=None))

    def test_unreadable_sidecar_fails_open(self):
        self._sidecar(raw="{not json")
        self.assertIn("accepted", self._ping())
        self._sidecar(raw=json.dumps({"letter_id": 6020, "reply_id": 6022, "to": "CC",
                                      "replied_at": "yesterday-ish"}))
        self.assertIn("accepted", self._ping())
        self.assertEqual(len(_StubDispatcher.received), 2)

    def test_letter_file_itself_is_not_mistaken_for_a_sidecar(self):
        (self.inbox / "msg-6020-20261105T210804.json").write_text(json.dumps({"id": 6020}))
        self.assertIn("accepted", self._ping())


if __name__ == "__main__":
    unittest.main()
