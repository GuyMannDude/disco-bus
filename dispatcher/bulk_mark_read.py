#!/usr/bin/env python3
"""Bulk mark-read for automated bus traffic.

An inbox that is 70% machine-generated digests stops being read at all, and the
one letter that mattered goes down with the noise. This clears the digests so
the authored messages are what is left.

The rule that makes it safe: a message is marked read ONLY if its subject starts
with a pattern in FAMILIES below. Everything else is left alone, including
anything an agent wrote by hand. There is no "mark the rest" mode, on purpose —
an unanchored substring match on 'cron' or 'daily' catches subjects like
'key-rotation-done-not-just-the-crontab', which is exactly the letter you cannot
afford to bury.

Dry run by default. --apply writes.

    python3 bulk_mark_read.py --agent Opie --before 2026-08-04
    python3 bulk_mark_read.py --agent Opie --before 2026-08-04 --apply
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("DISCOBUS_DB", str(Path.home() / ".disco-bus" / "disco-bus.sqlite")))

# (family name, subject prefix). Anchored at the start of the subject, always.
# Adding a family means proving on real data that the pattern catches nothing a
# person wrote — run --list-subjects and read them.
FAMILIES = [
    ("sec-watch digest", "sec-watch:"),
    ("sec-watch feed digest", "sec-watch-feeds:"),
    ("brain drift alert", "igor-brain-drift-detected"),
    ("discord liveness probe", "discord-liveness-"),
    ("cronalarm daily report", "cronalarm-daily-report-"),
    ("dream contradiction report", "dream-contradictions-"),
]


# Carved out of the sweep even though they match a family above. The point of
# clearing digests is to make the unread count mean something again; a critical
# security digest nobody opened is exactly the thing that count should still be
# pointing at. Three of these were sitting in the 2026-08-11 backlog.
NEVER_SWEEP = [":critical:"]


def matching_rows(conn, agent, before):
    """Unread messages for `agent`, created before `before`, matching a family."""
    clauses = " OR ".join("subject LIKE ?" for _ in FAMILIES)
    params = [agent, before] + [f"{prefix}%" for _, prefix in FAMILIES]
    rows = conn.execute(
        f"""SELECT id, from_agent, subject, created_at FROM messages
            WHERE to_agent = ? AND read_at IS NULL AND date(created_at) < ?
              AND ({clauses})
            ORDER BY created_at""",
        params,
    ).fetchall()
    return [r for r in rows if family_of(r["subject"])]


def family_of(subject):
    if any(marker in subject for marker in NEVER_SWEEP):
        return None
    for name, prefix in FAMILIES:
        if subject.startswith(prefix):
            return name
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", required=True, help="recipient whose inbox to clear")
    ap.add_argument("--before", required=True, help="YYYY-MM-DD, exclusive — nothing on or after is touched")
    ap.add_argument("--apply", action="store_true", help="write; omit for a dry run")
    ap.add_argument("--list-subjects", action="store_true", help="print every subject that would be marked")
    args = ap.parse_args()

    try:
        datetime.strptime(args.before, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"--before must be YYYY-MM-DD, got {args.before!r}")

    if not DB_PATH.exists():
        sys.exit(f"no bus database at {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = matching_rows(conn, args.agent, args.before)
    total_unread = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE to_agent = ? AND read_at IS NULL", (args.agent,)
    ).fetchone()[0]

    counts = {}
    for row in rows:
        counts[family_of(row["subject"])] = counts.get(family_of(row["subject"]), 0) + 1

    print(f"{args.agent}: {total_unread} unread, {len(rows)} match an automated family before {args.before}")
    for name, _ in FAMILIES:
        if counts.get(name):
            print(f"  {counts[name]:>4}  {name}")
    print(f"  ----")
    print(f"  {total_unread - len(rows):>4}  LEFT UNREAD (authored, or on/after {args.before})")

    if args.list_subjects:
        print()
        for row in rows:
            print(f"  #{row['id']:<6} {row['created_at'][:10]}  {row['from_agent']:>10} > {row['subject'][:80]}")

    if not rows:
        return
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.executemany(
            "UPDATE messages SET read_at = ? WHERE id = ? AND read_at IS NULL",
            [(stamp, row["id"]) for row in rows],
        )
    remaining = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE to_agent = ? AND read_at IS NULL", (args.agent,)
    ).fetchone()[0]
    print(f"\nMarked {len(rows)} read at {stamp}. {remaining} still unread.")


if __name__ == "__main__":
    main()
