import importlib.util
import json
import sqlite3
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


class ReadStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        dispatcher.DB_PATH = Path(self.tempdir.name) / "bus.sqlite"
        dispatcher.init_db()
        conn = dispatcher.get_db()
        conn.executemany(
            """INSERT INTO messages
               (mesh_version, tracking_id, from_agent, to_agent, reply_to,
                subject, body, state, created_at)
               VALUES ('0.5', ?, ?, ?, ?, ?, '{}', 'DELIVERED', ?)""",
            [
                ("msg-1-test", "Rocky", "Opie", None, "broadcast", "2026-07-24T00:00:00Z"),
                ("msg-2-test", "Dave", "Rocky", 1, "other-agent-reply", "2026-07-24T00:01:00Z"),
            ],
        )
        conn.commit()
        conn.close()
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

    def test_ping_read_separates_read_from_reply_state(self):
        # Listing does not mark read.
        self.assertEqual(
            [m["id"] for m in self.get_json("/mesh/inbox/Opie?filter=unread")],
            [1],
        )
        self.assertIsNone(self.get_json("/mesh/state/1")["read_at"])

        first = self.post_json("/mesh/read/1", {"agent": "Opie"})
        self.assertIsNotNone(first["read_at"])
        self.assertEqual(self.get_json("/mesh/inbox/Opie?filter=unread"), [])

        # Reading does not count as replying.
        self.assertEqual(
            [m["id"] for m in self.get_json("/mesh/inbox/Opie?filter=unreplied")],
            [1],
        )

        # Later reads preserve the first timestamp.
        second = self.post_json("/mesh/read/1", {"agent": "Opie"})
        self.assertEqual(second["read_at"], first["read_at"])

    def test_legacy_unread_only_still_means_unreplied(self):
        self.assertEqual(
            [m["id"] for m in self.get_json("/mesh/inbox/Opie")],
            [1],
        )
        legacy = self.get_json("/mesh/inbox/Opie?unread_only=true")
        explicit = self.get_json("/mesh/inbox/Opie?filter=unreplied")
        self.assertEqual([m["id"] for m in legacy], [m["id"] for m in explicit])

        conn = dispatcher.get_db()
        conn.execute(
            """INSERT INTO messages
               (mesh_version, tracking_id, from_agent, to_agent, reply_to,
                subject, body, state, created_at)
               VALUES ('0.5', 'msg-3-test', 'Opie', 'Rocky', 1,
                       'reply', '{}', 'DELIVERED', '2026-07-24T00:02:00Z')"""
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.get_json("/mesh/inbox/Opie?unread_only=true"), [])
        self.assertEqual(self.get_json("/mesh/inbox/Opie?filter=unreplied"), [])

    def test_first_read_distinguishes_this_call_from_an_earlier_one(self):
        # The bug: first and subsequent reads returned byte-identical payloads,
        # so a reader seeing read_at could not tell whether its own call had
        # just set it. Opie read the timestamp he had created and reported
        # "already read" on brand-new mail.
        first = self.post_json("/mesh/read/1", {"agent": "Opie"})
        self.assertTrue(first["first_read"])

        second = self.post_json("/mesh/read/1", {"agent": "Opie"})
        self.assertFalse(second["first_read"])

        # Guard the actual symptom, not just the flag: the two payloads must
        # no longer be indistinguishable.
        self.assertNotEqual(first, second)
        self.assertEqual(second["read_at"], first["read_at"])

    def test_first_read_flag_is_absent_from_listings(self):
        # first_read describes a read call, not the message. Listing endpoints
        # must not sprout it — inbox/history/state never mark anything read.
        self.post_json("/mesh/read/1", {"agent": "Opie"})
        self.assertNotIn("first_read", self.get_json("/mesh/state/1"))
        for message in self.get_json("/mesh/inbox/Opie?filter=all"):
            self.assertNotIn("first_read", message)

    def test_only_recipient_can_mark_read(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post_json("/mesh/read/1", {"agent": "Rocky"})
        self.assertEqual(caught.exception.code, 403)
        self.assertIsNone(self.get_json("/mesh/state/1")["read_at"])


class MigrationTests(unittest.TestCase):
    def test_existing_rows_are_not_backfilled(self):
        with tempfile.TemporaryDirectory() as tempdir:
            dispatcher.DB_PATH = Path(tempdir) / "old.sqlite"
            conn = sqlite3.connect(dispatcher.DB_PATH)
            conn.executescript(dispatcher.SCHEMA.replace(",\n    read_at TEXT", ""))
            conn.execute(
                """INSERT INTO messages
                   (mesh_version, tracking_id, from_agent, to_agent, subject,
                    body, state, created_at)
                   VALUES ('0.5', 'old', 'Rocky', 'Opie', 'old', '{}',
                           'DELIVERED', '2026-07-23T00:00:00Z')"""
            )
            conn.commit()
            conn.close()

            dispatcher.init_db()
            conn = dispatcher.get_db()
            row = conn.execute(
                "SELECT read_at FROM messages WHERE tracking_id='old'"
            ).fetchone()
            conn.close()
            self.assertIsNone(row["read_at"])


if __name__ == "__main__":
    unittest.main()
