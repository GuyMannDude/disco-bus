import importlib.util
import json
import sys
import tempfile
import threading
import time
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


class SinkAckTests(unittest.TestCase):
    """A reply TO a sink is an ACK, not mail (Guy 2026-10-16).

    Sinks (sec-watch, cron-report, ...) have no inbox: port 9 is discard, so
    any delivery to them FAILS and lights the IRIS failed badge for a letter
    that was in fact handled. The fix: a reply whose target is a sink is
    born ACKED + swept, never delivered, and the parent letter is stamped
    cleared_reason='ack:#<id>' so a board can show "seen and ruled".
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        dispatcher.DB_PATH = Path(self.tempdir.name) / "bus.sqlite"
        dispatcher.AGENTS_PATH = Path(self.tempdir.name) / "agents.json"
        dispatcher.AGENTS_PATH.write_text(json.dumps({
            # Port-9 convention (the live registry's shape) and the explicit flag.
            "sec-watch": {"url": "http://127.0.0.1:9/inbox"},
            "print-watch": {"url": "http://127.0.0.1:65000/inbox", "sink": True},
            # Real agents: no URL, so delivery FAILS in the background thread,
            # which is irrelevant here — ack rows never reach deliver().
            "CC": {},
            "Opie": {},
        }))
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
            "from": "sec-watch",
            "to": "CC",
            "subject": "sec-watch:hot: something",
            "body": {"tier": "hot"},
        }
        envelope.update(extra)
        return self.post_json("/mesh/ping", envelope)

    def ack(self, parent_id, **extra):
        return self.ping(**{
            "from": "CC", "to": "sec-watch", "reply_to": parent_id,
            "subject": "re: handled", "body": {"verdict": "nltk accepted"},
            **extra,
        })

    def test_is_sink_by_port_and_by_flag(self):
        self.assertTrue(dispatcher.is_sink({"url": "http://127.0.0.1:9/inbox"}))
        self.assertTrue(dispatcher.is_sink({"url": "http://127.0.0.1:65000/inbox", "sink": True}))
        self.assertFalse(dispatcher.is_sink({"url": "http://127.0.0.1:9131/inbox"}))
        self.assertFalse(dispatcher.is_sink({}))
        self.assertFalse(dispatcher.is_sink(None))

    def test_reply_to_sink_is_acked_not_delivered(self):
        parent = self.ping()
        receipt = self.ack(parent["id"])
        self.assertEqual(receipt["state"], "ACKED")
        self.assertEqual(receipt["ack_of"], parent["id"])

        ack_row = self.get_json(f"/mesh/state/{receipt['id']}")
        self.assertEqual(ack_row["state"], "ACKED")
        self.assertIsNone(ack_row["delivery_error"])
        self.assertIsNone(ack_row["delivered_at"])
        self.assertEqual(ack_row["cleared_reason"], "ack")
        self.assertEqual(ack_row["cleared_at"], ack_row["created_at"])
        self.assertEqual(ack_row["reply_to"], parent["id"])

        parent_row = self.get_json(f"/mesh/state/{parent['id']}")
        self.assertEqual(parent_row["cleared_reason"], f"ack:#{receipt['id']}")
        self.assertIsNotNone(parent_row["cleared_at"])
        # An ack implies opened: a critical must never leave unread unopened.
        self.assertIsNotNone(parent_row["read_at"])

        # Give the (non-existent) delivery thread a moment: the ack must never
        # show up on the failed-delivery surface IRIS polls.
        time.sleep(0.2)
        failed_ids = [m["id"] for m in self.get_json("/mesh/failed?since_id=0")]
        self.assertNotIn(receipt["id"], failed_ids)

    def test_ack_appears_on_acks_feed(self):
        parent = self.ping()
        receipt = self.ack(parent["id"])
        acks = self.get_json("/mesh/acks?since_id=0")
        self.assertEqual([a["id"] for a in acks], [receipt["id"]])
        self.assertEqual(acks[0]["ack_of"], parent["id"])
        self.assertEqual(acks[0]["parent_subject"], "sec-watch:hot: something")
        self.assertEqual(self.get_json(f"/mesh/acks?since_id={receipt['id']}"), [])

    def test_explicit_sink_flag_acks_too(self):
        parent = self.ping(**{"from": "print-watch"})
        receipt = self.ack(parent["id"], to="print-watch")
        self.assertEqual(receipt["state"], "ACKED")

    def test_only_recipient_can_ack(self):
        parent = self.ping()  # sec-watch -> CC
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.ack(parent["id"], **{"from": "Opie"})
        self.assertEqual(ctx.exception.code, 403)
        # And the parent is untouched.
        self.assertIsNone(self.get_json(f"/mesh/state/{parent['id']}")["cleared_at"])

    def test_ack_must_point_at_a_letter_from_that_sink(self):
        parent = self.ping(**{"from": "Opie"})  # Opie -> CC, not a sink letter
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.ack(parent["id"])  # CC -> sec-watch, reply_to Opie's letter
        self.assertEqual(ctx.exception.code, 400)

    def test_second_ack_is_409_and_record_keeps_the_first(self):
        parent = self.ping()
        first = self.ack(parent["id"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.ack(parent["id"], subject="re: again")
        self.assertEqual(ctx.exception.code, 409)
        parent_row = self.get_json(f"/mesh/state/{parent['id']}")
        self.assertEqual(parent_row["cleared_reason"], f"ack:#{first['id']}")
        self.assertEqual(len(self.get_json("/mesh/acks?since_id=0")), 1)

    def test_ack_of_missing_parent_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.ack(999)
        self.assertEqual(ctx.exception.code, 404)

    def test_fresh_letter_to_sink_still_takes_the_old_path(self):
        # Without reply_to there is nothing to ack: SENT, then the background
        # delivery FAILS as it always has (port 9 is discard by design).
        receipt = self.ping(**{"from": "CC", "to": "sec-watch", "subject": "hello sink"})
        self.assertEqual(receipt["state"], "SENT")

    def test_reply_to_a_real_agent_is_unchanged(self):
        parent = self.ping(**{"from": "Opie", "to": "CC"})
        receipt = self.ping(**{"from": "CC", "to": "Opie", "reply_to": parent["id"],
                               "subject": "re: hi", "body": {}})
        self.assertEqual(receipt["state"], "SENT")
        self.assertNotIn("ack_of", receipt)
        self.assertIsNone(self.get_json(f"/mesh/state/{parent['id']}")["cleared_at"])


if __name__ == "__main__":
    unittest.main()
