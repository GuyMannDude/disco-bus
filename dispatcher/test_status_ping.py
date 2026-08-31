import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("dispatcher.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("disco_bus_dispatcher", MODULE_PATH)
dispatcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dispatcher)


class StatusPingTests(unittest.TestCase):
    """class=status envelopes are born swept: never unread, never opened.

    Born from snag heartbeat-pings-pollute-unread (2026-08-31): a daily
    liveness ping sat permanently unread in Opie's inbox because only the
    recipient can mark mail read and the recipient is right to never open
    furniture. A status ping must leave the unread view at birth while
    read_at stays NULL and delivery/search behave like any other envelope.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        dispatcher.DB_PATH = Path(self.tempdir.name) / "bus.sqlite"
        dispatcher.AGENTS_PATH = Path(self.tempdir.name) / "agents.json"
        # No listener URLs: delivery FAILS in the background thread, which is
        # irrelevant here — cleared_at is written at insert, before delivery.
        dispatcher.AGENTS_PATH.write_text(
            json.dumps({"Opie": {}, "discord-liveness": {}})
        )
        dispatcher.init_db()
        self.server = dispatcher.ThreadingHTTPServer(
            ("127.0.0.1", 0), dispatcher.DispatcherHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def post_json(self, path, data):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def ping(self, **extra):
        envelope = {
            "mesh_version": "0.5",
            "from": "discord-liveness",
            "to": "Opie",
            "subject": "discord-liveness-2026-08-31-alive",
            "body": {"verdict": "ALIVE"},
        }
        envelope.update(extra)
        return self.post_json("/mesh/ping", envelope)

    def test_status_ping_is_born_swept_but_present(self):
        sent = self.ping(**{"class": "status"})
        msg_id = sent["id"]

        # Absent from the unread view (this is the badge IRIS renders)...
        self.assertEqual(self.get_json("/mesh/inbox/Opie?filter=unread"), [])

        # ...but PRESENT, swept-not-read: the row exists, cleared at birth,
        # and read_at still truthfully says nobody opened it.
        state = self.get_json(f"/mesh/state/{msg_id}")
        self.assertEqual(state["cleared_at"], state["created_at"])
        self.assertEqual(state["cleared_reason"], "status-ping")
        self.assertIsNone(state["read_at"])

    def test_ordinary_ping_still_counts_unread(self):
        # The control: without class=status the same envelope must land
        # unread, or the pass above proves nothing about the class field.
        sent = self.ping()
        unread = [m["id"] for m in self.get_json("/mesh/inbox/Opie?filter=unread")]
        self.assertEqual(unread, [sent["id"]])
        self.assertIsNone(self.get_json(f"/mesh/state/{sent['id']}")["cleared_at"])

    def test_unknown_class_is_rejected_loudly(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.ping(**{"class": "heartbeat"})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
