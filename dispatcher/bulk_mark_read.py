#!/usr/bin/env python3
"""Bulk-clear automated bus traffic from an inbox — without lying about reads.

An inbox that is 70% machine-generated digests stops being read at all, and the
one letter that mattered goes down with the noise. This clears the digests so
the authored messages are what is left.

Cleared is NOT read (Opie #2835, 2026-08-22). This tool never touches read_at:
it stamps cleared_at + cleared_reason='bulk', so the audit record says "nobody
read this, it was swept" instead of falsely claiming the recipient opened it.
The unread view (dispatcher /mesh/inbox filter=unread) excludes both read and
cleared rows; read_at keeps meaning exactly what it says.

The rule that makes it safe: a message is cleared ONLY if its subject starts
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
    ("wedge watch report", "mnemo-wedge-watch-"),
]


# Carved out of the sweep even though they match a family above. The point of
# clearing digests is to make the unread count mean something again; a critical
# security digest nobody opened is exactly the thing that count should still be
# pointing at. Three of these were sitting in the 2026-08-11 backlog.
NEVER_SWEEP = [":critical:"]


# Must match the dispatcher's filter=unread predicate exactly. HELD/DROPPED
# are undelivered drafts, not mail — sweeping a HELD row would make it
# permanently invisible after /mesh/release delivers it (review 2026-08-22).
UNREAD = ("read_at IS NULL AND cleared_at IS NULL "
          "AND state NOT IN ('HELD','DROPPED')")


def matching_rows(conn, agent, before, include_authored=False):
    """Unread, uncleared messages for `agent`, created before `before`.

    Default: only subjects matching an automated FAMILY. With
    include_authored=True (Opie #2835: "drain the standing backlog"), ALL
    unread before the date are swept — honest only because the write path
    stamps cleared_at, never read_at. NEVER_SWEEP still holds either way."""
    if include_authored:
        rows = conn.execute(
            f"""SELECT id, from_agent, subject, created_at FROM messages
                WHERE to_agent = ? AND {UNREAD} AND date(created_at) < ?
                ORDER BY created_at""",
            (agent, before),
        ).fetchall()
        return [r for r in rows
                if not any(marker in r["subject"] for marker in NEVER_SWEEP)]
    clauses = " OR ".join("subject LIKE ?" for _ in FAMILIES)
    params = [agent, before] + [f"{prefix}%" for _, prefix in FAMILIES]
    rows = conn.execute(
        f"""SELECT id, from_agent, subject, created_at FROM messages
            WHERE to_agent = ? AND {UNREAD} AND date(created_at) < ?
              AND ({clauses})
            ORDER BY created_at""",
        params,
    ).fetchall()
    return [r for r in rows if family_of(r["subject"])]


def clear(conn, rows):
    """Stamp cleared_at + cleared_reason='bulk' on `rows`. Never read_at:
    nobody opened these, and the record must say so."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.executemany(
            f"UPDATE messages SET cleared_at = ?, cleared_reason = 'bulk' "
            f"WHERE id = ? AND {UNREAD}",
            [(stamp, row["id"]) for row in rows],
        )
    return stamp


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
    ap.add_argument("--include-authored", action="store_true",
                    help="sweep ALL unread before the date, not just automated families "
                         "(Opie #2835 backlog drain); ':critical:' is still never swept")
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

    rows = matching_rows(conn, args.agent, args.before, args.include_authored)
    total_unread = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE to_agent = ? AND {UNREAD}", (args.agent,)
    ).fetchone()[0]

    counts = {}
    for row in rows:
        family = family_of(row["subject"]) or "authored / unmatched"
        counts[family] = counts.get(family, 0) + 1

    scope = "unread (authored included)" if args.include_authored else "match an automated family"
    print(f"{args.agent}: {total_unread} unread, {len(rows)} {scope} before {args.before}")
    for name in [n for n, _ in FAMILIES] + ["authored / unmatched"]:
        if counts.get(name):
            print(f"  {counts[name]:>4}  {name}")
    print(f"  ----")
    print(f"  {total_unread - len(rows):>4}  LEFT UNREAD (authored, on/after {args.before}, or protected)")

    if args.list_subjects:
        print()
        for row in rows:
            print(f"  #{row['id']:<6} {row['created_at'][:10]}  {row['from_agent']:>10} > {row['subject'][:80]}")

    if not rows:
        return
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    stamp = clear(conn, rows)
    remaining = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE to_agent = ? AND {UNREAD}", (args.agent,)
    ).fetchone()[0]
    print(f"\nCleared {len(rows)} (cleared_at={stamp}, cleared_reason=bulk, "
          f"read_at untouched). {remaining} still unread.")


if __name__ == "__main__":
    main()
