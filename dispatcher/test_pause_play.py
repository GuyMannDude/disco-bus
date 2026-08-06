"""IRIS phase 2 — dispatcher-enforced pause/play (spec bus-lamp-pause-spec.md §2).

The guardrails under test:
  - a paused sender's ping lands HELD, undelivered (enforced by the bus, not promises)
  - HELD/DROPPED never appear in any inbox view, and a HELD reply does not
    clear unreplied
  - play flushes held messages IN ID ORDER to the real listener
  - release ships exactly one held message while still paused; drop parks it
    as DROPPED (kept in history, never delivered)
  - sender-only: no other agent can release or drop a held message
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("dispatcher.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("disco_bus_dispatcher_pause", MODULE_PATH)
dispatcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dispatcher)


class RecordingListener(BaseHTTPRequestHandler):
    """Accepts every envelope, records arrival order."""

    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        envelope = json.loads(self.rfile.read(length))
        RecordingListener.received.append(envelope)
        body = json.dumps({"state": "DELIVERED"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class PausePlayTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        dispatcher.DB_PATH = Path(self.tempdir.name) / "bus.sqlite"
        dispatcher.init_db()

        RecordingListener.received = []
        self.listener = ThreadingHTTPServer(("127.0.0.1", 0), RecordingListener)
        self.listener_thread = threading.Thread(
            target=self.listener.serve_forever, daemon=True
        )
        self.listener_thread.start()

        dispatcher.AGENTS_PATH = Path(self.tempdir.name) / "agents.json"
        dispatcher.AGENTS_PATH.write_text(json.dumps({
            "CC": {"url": f"http://127.0.0.1:{self.listener.server_port}/"},
            "Opie": {"url": f"http://127.0.0.1:{self.listener.server_port}/"},
        }))

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
        self.listener.shutdown()
        self.listener.server_close()
        self.listener_thread.join(timeout=2)
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

    def ping(self, subject, reply_to=None):
        return self.post_json("/mesh/ping", {
            "mesh_version": "0.5", "from": "CC", "to": "Opie",
            "subject": subject, "body": {}, "reply_to": reply_to,
        })

    def wait_for_deliveries(self, n, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(RecordingListener.received) >= n:
                return
            time.sleep(0.02)
        self.fail(f"expected {n} deliveries, got {len(RecordingListener.received)}")

    def db_state(self, msg_id):
        return self.get_json(f"/mesh/state/{msg_id}")["state"]

    def wait_for_state(self, msg_ids, state, timeout=5.0):
        """Deadline-poll the DB: the listener's 200 races update_state's
        commit, so asserting DELIVERED immediately after the POST arrives is
        flaky (review 2026-08-06 #2 finding 2 — reproduced 1-in-6)."""
        deadline = time.monotonic() + timeout
        states = {}
        while time.monotonic() < deadline:
            states = {i: self.db_state(i) for i in msg_ids}
            if all(s == state for s in states.values()):
                return
            time.sleep(0.02)
        self.fail(f"expected {state} for {msg_ids}, got {states}")

    def test_ping_while_paused_lands_held_and_undelivered(self):
        self.post_json("/mesh/pause", {"agent": "CC"})
        result = self.ping("held-one")
        self.assertEqual(result["state"], "HELD")
        time.sleep(0.3)  # a delivery would be near-instant against localhost
        self.assertEqual(RecordingListener.received, [])
        self.assertEqual(self.db_state(result["id"]), "HELD")

        # HELD is invisible to every inbox view but present in held/pause.
        for view in ("unread", "unreplied", "all"):
            self.assertEqual(self.get_json(f"/mesh/inbox/Opie?filter={view}"), [])
        held = self.get_json("/mesh/held/CC")
        self.assertEqual([m["id"] for m in held], [result["id"]])
        pause = self.get_json("/mesh/pause")
        self.assertEqual(pause["paused"][0]["agent"], "CC")
        self.assertEqual(pause["paused"][0]["held"], 1)

    def test_held_reply_does_not_clear_unreplied(self):
        inbound = self.post_json("/mesh/ping", {
            "mesh_version": "0.5", "from": "Opie", "to": "CC",
            "subject": "question", "body": {},
        })
        self.wait_for_deliveries(1)
        self.post_json("/mesh/pause", {"agent": "CC"})
        self.ping("held-reply", reply_to=inbound["id"])
        unreplied = [m["id"] for m in self.get_json("/mesh/inbox/CC?filter=unreplied")]
        self.assertEqual(unreplied, [inbound["id"]])

    def test_play_flushes_in_id_order(self):
        self.post_json("/mesh/pause", {"agent": "CC"})
        ids = [self.ping(f"held-{i}")["id"] for i in range(3)]
        result = self.post_json("/mesh/play", {"agent": "CC"})
        self.assertFalse(result["paused"])
        self.assertEqual([m["id"] for m in result["released"]], ids)
        self.wait_for_deliveries(3)
        self.assertEqual([e["id"] for e in RecordingListener.received], ids)
        self.wait_for_state(ids, "DELIVERED")
        self.assertEqual(self.get_json("/mesh/pause"), {"paused": [], "held_orphans": []})
        # Delivered mail is back in the recipient's inbox.
        self.assertEqual(
            sorted(m["id"] for m in self.get_json("/mesh/inbox/Opie?filter=unread")),
            ids,
        )

    def test_release_ships_one_while_still_paused(self):
        self.post_json("/mesh/pause", {"agent": "CC"})
        hot = self.ping("hot-item")["id"]
        cold = self.ping("cold-item")["id"]
        self.post_json(f"/mesh/release/{hot}", {"agent": "CC"})
        self.wait_for_deliveries(1)
        self.assertEqual(RecordingListener.received[0]["id"], hot)
        self.wait_for_state([hot], "DELIVERED")
        self.assertEqual(self.db_state(cold), "HELD")
        # Still paused: the next ping is still held.
        self.assertEqual(self.ping("still-held")["state"], "HELD")

    def test_drop_parks_without_delivering(self):
        self.post_json("/mesh/pause", {"agent": "CC"})
        msg_id = self.ping("drop-me")["id"]
        dropped = self.post_json(f"/mesh/drop/{msg_id}", {"agent": "CC"})
        self.assertEqual(dropped["state"], "DROPPED")
        self.post_json("/mesh/play", {"agent": "CC"})
        time.sleep(0.3)
        self.assertEqual(RecordingListener.received, [])
        # Kept in history (never erased), absent from every inbox view.
        self.assertEqual(self.db_state(msg_id), "DROPPED")
        self.assertEqual(self.get_json("/mesh/inbox/Opie?filter=all"), [])

    def test_only_sender_can_release_or_drop(self):
        self.post_json("/mesh/pause", {"agent": "CC"})
        msg_id = self.ping("mine")["id"]
        for action in ("release", "drop"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post_json(f"/mesh/{action}/{msg_id}", {"agent": "Opie"})
            self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.db_state(msg_id), "HELD")

    def test_release_and_drop_refuse_non_held(self):
        sent = self.ping("normal")  # not paused
        self.wait_for_deliveries(1)
        for action in ("release", "drop"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post_json(f"/mesh/{action}/{sent['id']}", {"agent": "CC"})
            self.assertEqual(ctx.exception.code, 409)

    def test_pause_rejects_unknown_agent(self):
        for verb in ("pause", "play"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post_json(f"/mesh/{verb}", {"agent": "Nobody"})
            self.assertEqual(ctx.exception.code, 400)

    def test_orphaned_held_is_reported_not_hidden(self):
        self.post_json("/mesh/pause", {"agent": "CC"})
        self.ping("orphan-to-be")
        # Simulate the crash window: pause row gone, HELD row left behind.
        conn = sqlite3.connect(dispatcher.DB_PATH)
        conn.execute("DELETE FROM pauses WHERE agent='CC'")
        conn.commit()
        conn.close()
        pause = self.get_json("/mesh/pause")
        self.assertEqual(pause["paused"], [])
        self.assertEqual(pause["held_orphans"], [{"agent": "CC", "held": 1}])


if __name__ == "__main__":
    unittest.main()
