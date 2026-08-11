"""Tests for bulk_mark_read.

The load-bearing test is test_human_authored_subject_is_never_marked. Its
fixtures are real subjects from Opie's backlog that an unanchored 'cron'/'daily'
match would have swept — including one about key rotation.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import bulk_mark_read

# Real subjects from the 2026-08-11 backlog audit. Every one of these contains a
# substring that reads as automated. Every one was written by an agent by hand.
HUMAN_AUTHORED = [
    "key-rotation-done-not-just-the-crontab",
    "cronalarm-v1.2-shipped: textbelt sms removed",
    "nightly-rotation LIVE: gallery posts itself at 00:05",
    "re-ping closeout: banner + logging already done",
    "re: spec: rocky-sec-watch-cron",
    "shipped: rocky-sec-watch-cron (real spec-back, supersedes #150)",
]

AUTOMATED = [
    "sec-watch:quiet: Daily OSV feed: npm/PyPI malicious packages",
    "sec-watch-feeds:hot: npm supply-chain attack",
    "igor-brain-drift-detected",
    "discord-liveness-2026-07-25-alive",
    "cronalarm-daily-report-2026-07-27-all-clear",
    "dream-contradictions-2026-05-20",
]


class BulkMarkReadTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        bulk_mark_read.DB_PATH = Path(self.tempdir.name) / "bus.sqlite"
        self.conn = sqlite3.connect(bulk_mark_read.DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
                subject TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT)"""
        )

    def add(self, subject, to_agent="Opie", created_at="2026-07-01T00:00:00Z", read_at=None):
        cur = self.conn.execute(
            "INSERT INTO messages (from_agent, to_agent, subject, created_at, read_at) VALUES ('CC',?,?,?,?)",
            (to_agent, subject, created_at, read_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def matched_subjects(self, agent="Opie", before="2026-08-04"):
        return {r["subject"] for r in bulk_mark_read.matching_rows(self.conn, agent, before)}

    def test_human_authored_subject_is_never_marked(self):
        for subject in HUMAN_AUTHORED:
            self.add(subject)
        self.assertEqual(self.matched_subjects(), set())

    def test_automated_families_are_matched(self):
        for subject in AUTOMATED:
            self.add(subject)
        self.assertEqual(self.matched_subjects(), set(AUTOMATED))

    def test_before_date_is_exclusive(self):
        self.add("igor-brain-drift-detected", created_at="2026-08-03T23:59:00Z")
        self.add("igor-brain-drift-detected", created_at="2026-08-04T00:01:00Z")
        self.assertEqual(len(bulk_mark_read.matching_rows(self.conn, "Opie", "2026-08-04")), 1)

    def test_other_recipients_are_untouched(self):
        self.add("igor-brain-drift-detected", to_agent="Rocky")
        self.assertEqual(self.matched_subjects(agent="Opie"), set())

    def test_already_read_messages_are_not_rematched(self):
        self.add("igor-brain-drift-detected", read_at="2026-06-01T00:00:00Z")
        self.assertEqual(self.matched_subjects(), set())

    def test_critical_digests_are_never_swept(self):
        self.add("sec-watch:critical: PyPI package 'fastapi' compromised")
        self.add("sec-watch-feeds:critical: active exploit in the wild")
        self.assertEqual(self.matched_subjects(), set())

    def test_family_of_returns_none_for_authored(self):
        for subject in HUMAN_AUTHORED:
            self.assertIsNone(bulk_mark_read.family_of(subject), subject)


if __name__ == "__main__":
    unittest.main()
