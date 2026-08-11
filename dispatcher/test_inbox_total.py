"""The inbox must report how many messages EXIST, not how many it returned.

Opie #2339: `inbox` printed `len(rows)` as the count, so `limit=8` rendered as
"8 message(s)". A cap read as a measurement -- reported to Guy as a backlog
figure for three days while the real backlog was two orders of magnitude larger.

These tests are written so they FAIL against the old behaviour: each one asks a
question whose answer is indistinguishable from the page size unless the total
is measured independently of the limit.
"""

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("dispatcher.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("disco_bus_dispatcher", MODULE_PATH)
dispatcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dispatcher)

TOTAL_MESSAGES = 25


class InboxTotalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        dispatcher.DB_PATH = Path(self.tempdir.name) / "bus.sqlite"
        dispatcher.init_db()
        conn = dispatcher.get_db()
        # 25 unread to Opie, plus one HELD and one DROPPED that must never be
        # counted -- the total has to honour the same exclusions as the page.
        conn.executemany(
            """INSERT INTO messages
               (mesh_version, tracking_id, from_agent, to_agent, reply_to,
                subject, body, state, created_at)
               VALUES ('0.5', ?, 'CC', 'Opie', NULL, ?, '{}', ?, ?)""",
            [
                (f"msg-{i}-test", f"subject-{i}", "DELIVERED", f"2026-07-24T00:{i:02d}:00Z")
                for i in range(TOTAL_MESSAGES)
            ]
            + [
                ("msg-held-test", "held-one", "HELD", "2026-07-24T01:00:00Z"),
                ("msg-dropped-test", "dropped-one", "DROPPED", "2026-07-24T01:01:00Z"),
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

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response), dict(response.headers)

    def test_capped_page_still_reports_the_true_total(self):
        """THE REGRESSION TEST. Old behaviour could only say 5."""
        rows, headers = self.get("/mesh/inbox/Opie?filter=unread&limit=5")
        self.assertEqual(len(rows), 5, "page should honour the limit")
        self.assertEqual(headers["X-Inbox-Total"], str(TOTAL_MESSAGES))
        self.assertEqual(headers["X-Inbox-Returned"], "5")
        self.assertEqual(headers["X-Inbox-Truncated"], "true")

    def test_untruncated_page_is_not_flagged_as_truncated(self):
        """A guard that fires every day is worse than no guard."""
        rows, headers = self.get("/mesh/inbox/Opie?filter=unread&limit=100")
        self.assertEqual(len(rows), TOTAL_MESSAGES)
        self.assertEqual(headers["X-Inbox-Total"], str(TOTAL_MESSAGES))
        self.assertEqual(headers["X-Inbox-Truncated"], "false")

    def test_exact_boundary_is_not_truncated(self):
        """limit == total is the case a naive `len(rows) == limit` check calls
        truncated, and it is not."""
        _, headers = self.get(f"/mesh/inbox/Opie?filter=unread&limit={TOTAL_MESSAGES}")
        self.assertEqual(headers["X-Inbox-Truncated"], "false")
        self.assertEqual(headers["X-Inbox-Total"], str(TOTAL_MESSAGES))

    def test_total_honours_the_filter_not_just_the_recipient(self):
        """Count and page must share one predicate. Mark 20 read: `unread`
        must drop to 5 while `all` stays at 25."""
        conn = dispatcher.get_db()
        conn.execute(
            "UPDATE messages SET read_at='2026-07-25T00:00:00Z' WHERE id <= 20 AND state='DELIVERED'"
        )
        conn.commit()
        conn.close()
        _, unread = self.get("/mesh/inbox/Opie?filter=unread&limit=2")
        self.assertEqual(unread["X-Inbox-Total"], "5")
        _, everything = self.get("/mesh/inbox/Opie?filter=all&limit=2")
        self.assertEqual(everything["X-Inbox-Total"], str(TOTAL_MESSAGES))

    def test_total_excludes_held_and_dropped(self):
        """HELD/DROPPED are not mail. If the total counted them it would exceed
        what any page could ever return -- a permanently truncated inbox."""
        rows, headers = self.get("/mesh/inbox/Opie?filter=all&limit=500")
        self.assertEqual(headers["X-Inbox-Total"], str(TOTAL_MESSAGES))
        self.assertEqual(len(rows), TOTAL_MESSAGES)
        self.assertEqual(headers["X-Inbox-Truncated"], "false")

    def test_empty_inbox_reports_zero_not_truncated(self):
        _, headers = self.get("/mesh/inbox/Rocky?filter=unread&limit=5")
        self.assertEqual(headers["X-Inbox-Total"], "0")
        self.assertEqual(headers["X-Inbox-Truncated"], "false")

    def test_body_is_still_a_bare_array(self):
        """Non-breaking contract: every existing consumer reads a list."""
        rows, _ = self.get("/mesh/inbox/Opie?filter=unread&limit=3")
        self.assertIsInstance(rows, list)
        self.assertIn("tracking_id", rows[0])


if __name__ == "__main__":
    unittest.main()
