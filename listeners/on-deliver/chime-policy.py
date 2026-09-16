#!/usr/bin/env python3
"""Disco-Bus bell policy (v0.16): should THIS letter ring the human?

Reads the envelope JSON on stdin (what DISCOBUS_ON_DELIVER hands every
bell) and answers with an exit code the chime scripts and the listener
both understand:

    0  ring
    3  silent by policy (the listener logs it at info, not as a failure)

Anything else is a real failure. Rules, all optional, all from the env:

  DISCOBUS_CHIME_SKIP_FROM   comma-separated senders that never ring
                             (robots: a security feed, a cron reporter...)
  DISCOBUS_CHIME_COOLDOWN    seconds; after a ring, later letters inside
                             the window land silently. 0 (default) = off.
  DISCOBUS_CHIME_WAKE_RE     regex on the subject that rings ANYWAY —
                             through the skip list and the cooldown.
                             Default: the words WAKE or URGENT, upper-case.
  DISCOBUS_CHIME_STATE       file holding the last ring time. Default is
                             one file per machine (not per listener), so a
                             person watching three inboxes gets one bell.

An envelope that cannot be parsed RINGS: the letter is real even when the
bell is confused, and a silent failure is the one thing a bell must not do.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

RING, SILENT = 0, 3
DEFAULT_WAKE_RE = r"\b(WAKE|URGENT)\b"


def default_state_path() -> str:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    base = base or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "disco-bus", "chime-last-ring")


def read_last_ring(path: str) -> float:
    try:
        with open(path, encoding="utf-8") as fh:
            return float(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def write_last_ring(path: str, now: float) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{now:.3f}\n")
    except OSError:
        pass  # a bell that cannot remember still rings


def decide(env: dict, envelope: dict | None, now: float, state_path: str) -> tuple[bool, str]:
    """(ring?, reason). Pure apart from the state file."""
    if envelope is None:
        return True, "unparseable envelope: ring"
    subject = str(envelope.get("subject") or "")
    sender = str(envelope.get("from") or "")
    wake_re = env.get("DISCOBUS_CHIME_WAKE_RE") or DEFAULT_WAKE_RE
    try:
        wake = re.search(wake_re, subject) is not None
    except re.error:
        wake = re.search(DEFAULT_WAKE_RE, subject) is not None
    if wake:
        write_last_ring(state_path, now)
        return True, "wake marker in subject"
    skip = {s.strip() for s in (env.get("DISCOBUS_CHIME_SKIP_FROM") or "").split(",") if s.strip()}
    if sender in skip:
        return False, f"sender {sender} is on the skip list"
    try:
        cooldown = float(env.get("DISCOBUS_CHIME_COOLDOWN") or 0)
    except ValueError:
        cooldown = 0.0
    if cooldown > 0:
        since = now - read_last_ring(state_path)
        if 0 <= since < cooldown:
            return False, f"cooldown: rang {since:.0f}s ago, window {cooldown:.0f}s"
    write_last_ring(state_path, now)
    return True, "ring"


def main() -> int:
    raw = sys.stdin.read()
    try:
        envelope = json.loads(raw) if raw.strip() else None
        if not isinstance(envelope, dict):
            envelope = None
    except ValueError:
        envelope = None
    state = os.environ.get("DISCOBUS_CHIME_STATE") or default_state_path()
    ring, reason = decide(dict(os.environ), envelope, time.time(), state)
    print(reason, file=sys.stderr)
    return RING if ring else SILENT


if __name__ == "__main__":
    sys.exit(main())
