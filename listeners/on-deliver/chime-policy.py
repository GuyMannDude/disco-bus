#!/usr/bin/env python3
"""Disco-Bus bell policy (v0.16, hardened v0.17): should THIS letter ring the human?

Reads the envelope JSON on stdin (what DISCOBUS_ON_DELIVER hands every
bell) and answers with an exit code the chime scripts and the listener
both understand:

    0  ring
    3  silent by policy (the listener logs it at info, not as a failure)

Nothing else, ever — Windows chains `python chime-policy.py && schtasks ...`
on that promise. Stderr carries the reason when silent, and a complaint when
the bell rang on a setting it could not read (exit 0 + stderr = the listener
logs a warning). Rules, all optional, all from the env:

  DISCOBUS_CHIME_SKIP_FROM   comma-separated senders that never ring
                             (robots: a security feed, a cron reporter...);
                             matched case-insensitively, like agent names
                             everywhere else on the bus.
  DISCOBUS_CHIME_COOLDOWN    seconds; after a ring, later letters inside
                             the window land silently. 0 (default) = off.
  DISCOBUS_CHIME_WAKE_RE     regex on the subject that rings ANYWAY —
                             through the skip list and the cooldown.
                             Default: the words WAKE or URGENT, upper-case.
  DISCOBUS_CHIME_STATE       file holding the last ring time (`~` expands).
                             Default is one file per machine (not per
                             listener), so a person watching three inboxes
                             gets one bell. Three listeners racing on the
                             same second take turns through <state>.lock.

An envelope that cannot be parsed RINGS: the letter is real even when the
bell is confused, and a silent failure is the one thing a bell must not do.
"""
from __future__ import annotations

import contextlib
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


@contextlib.contextmanager
def state_lock(path: str):
    """Exclusive lock on <path>.lock across the whole read-decide-write, so
    three listeners (or three threads of one) landing letters in the same
    second agree on who rang. v0.17: unlocked, five concurrent bells rang
    three times. Cannot lock = proceed unlocked — a bell that cannot take
    turns still rings."""
    fh = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path + ".lock", "a+")
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
    except OSError:
        pass
    try:
        yield
    finally:
        if fh is not None:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def read_last_ring(path: str) -> float:
    try:
        with open(path, encoding="utf-8") as fh:
            return float(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def write_last_ring(path: str, now: float) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"{now:.3f}\n")
        os.replace(tmp, path)  # a reader never sees a half-written stamp
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)  # a bell that cannot remember still rings


def decide(env: dict, envelope: dict | None, now: float, state_path: str) -> tuple[bool, str, str]:
    """(ring?, reason, complaint). Pure apart from the state file. A
    complaint is non-empty when the bell rang but a setting was unreadable."""
    if envelope is None:
        return True, "unparseable envelope: ring", ""
    subject = str(envelope.get("subject") or "")
    sender = str(envelope.get("from") or "")
    wake_re = env.get("DISCOBUS_CHIME_WAKE_RE") or DEFAULT_WAKE_RE
    try:
        wake = re.search(wake_re, subject) is not None
    except re.error:
        wake = re.search(DEFAULT_WAKE_RE, subject) is not None
    if wake:
        write_last_ring(state_path, now)
        return True, "wake marker in subject", ""
    skip = {s.strip().lower() for s in (env.get("DISCOBUS_CHIME_SKIP_FROM") or "").split(",") if s.strip()}
    if sender.lower() in skip:
        return False, f"sender {sender} is on the skip list", ""
    complaint = ""
    raw_cooldown = env.get("DISCOBUS_CHIME_COOLDOWN") or "0"
    try:
        cooldown = float(raw_cooldown)
    except ValueError:
        cooldown = 0.0
        complaint = (f"bad DISCOBUS_CHIME_COOLDOWN {raw_cooldown!r}: cooldown OFF "
                     "(an inline # comment in an env file is part of the value)")
    with state_lock(state_path):
        if cooldown > 0:
            since = now - read_last_ring(state_path)
            # a stamp a few seconds in the FUTURE is the bell that beat us to
            # the lock, not a broken clock: inside the window either way
            if abs(since) < cooldown:
                return False, f"cooldown: rang {abs(since):.0f}s ago, window {cooldown:.0f}s", complaint
        write_last_ring(state_path, now)
    return True, "ring", complaint


def main() -> int:
    raw = sys.stdin.read()
    try:
        envelope = json.loads(raw) if raw.strip() else None
        if not isinstance(envelope, dict):
            envelope = None
    except ValueError:
        envelope = None
    state = os.path.expanduser(os.environ.get("DISCOBUS_CHIME_STATE") or default_state_path())
    ring, reason, complaint = decide(dict(os.environ), envelope, time.time(), state)
    if complaint:
        print(f"chime-policy: {complaint}", file=sys.stderr)
    if not ring:
        print(reason, file=sys.stderr)
    return RING if ring else SILENT


if __name__ == "__main__":
    sys.exit(main())
