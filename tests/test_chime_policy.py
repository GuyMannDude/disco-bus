"""v0.16 bell policy: robots land silently, a burst rings once, WAKE rings
through everything, and a bell that cannot read its letter still rings."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "listeners" / "on-deliver" / "chime-policy.py"
spec = importlib.util.spec_from_file_location("chime_policy", POLICY)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


def _env(**kw):
    return {"DISCOBUS_CHIME_SKIP_FROM": "feed-bot,cron-bot", "DISCOBUS_CHIME_COOLDOWN": "90", **kw}


def test_agents_ring_and_robots_do_not(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(), {"from": "beta", "subject": "re-12 done"}, 1000.0, st) == (True, "ring")
    ring, why = cp.decide(_env(), {"from": "feed-bot", "subject": "daily digest"}, 2000.0, st)
    assert not ring and "skip list" in why


def test_a_burst_rings_once_then_again_after_the_window(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(), {"from": "beta", "subject": "one"}, 1000.0, st)[0]
    ring, why = cp.decide(_env(), {"from": "gamma", "subject": "two"}, 1030.0, st)
    assert not ring and why.startswith("cooldown")
    assert cp.decide(_env(), {"from": "gamma", "subject": "three"}, 1091.0, st)[0]
    # cooldown off = every letter rings
    assert cp.decide(_env(DISCOBUS_CHIME_COOLDOWN="0"), {"from": "beta", "subject": "four"}, 1092.0, st)[0]


def test_wake_marker_rings_through_skip_list_and_cooldown(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(), {"from": "beta", "subject": "one"}, 1000.0, st)[0]
    ring, why = cp.decide(_env(), {"from": "cron-bot", "subject": "WAKE: job failed"}, 1001.0, st)
    assert ring and "wake" in why
    assert cp.decide(_env(), {"from": "beta", "subject": "urgent-ish"}, 1002.0, st)[0] is False  # lower-case, glued: not a marker
    assert cp.decide(_env(DISCOBUS_CHIME_WAKE_RE="(?i)poke"), {"from": "beta", "subject": "a Poke please"}, 1003.0, st)[0]
    assert cp.decide(_env(DISCOBUS_CHIME_WAKE_RE="(["), {"from": "beta", "subject": "URGENT"}, 1004.0, st)[0]  # bad regex -> default


def test_unparseable_letter_still_rings(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(), None, 1000.0, st) == (True, "unparseable envelope: ring")
    # end to end through the script: exit 0 = ring, 3 = silent
    env = {**_env(), "DISCOBUS_CHIME_STATE": st, "PATH": "/usr/bin:/bin"}
    def run(stdin: str) -> int:
        return subprocess.run([sys.executable, str(POLICY)], input=stdin, capture_output=True, text=True, env=env).returncode
    assert run("not json") == 0
    assert run(json.dumps({"from": "feed-bot", "subject": "x"})) == 3
    assert run(json.dumps({"from": "beta", "subject": "x"})) == 0
    assert run(json.dumps({"from": "beta", "subject": "y"})) == 3   # inside the window
    assert run(json.dumps({"from": "beta", "subject": "WAKE"})) == 0


def test_listener_logs_silence_as_info_not_warning(tmp_path, caplog, monkeypatch):
    import importlib
    sys.path.insert(0, str(ROOT / "listeners"))
    monkeypatch.setenv("DISCOBUS_ON_DELIVER", "exit 3")
    listener = importlib.reload(importlib.import_module("listener"))
    import logging
    with caplog.at_level(logging.INFO):
        listener.run_on_deliver({"from": "beta", "subject": "x"})
    assert any("silent by policy" in r.message and r.levelno == logging.INFO for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
