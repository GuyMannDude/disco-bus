"""v0.16 bell policy: robots land silently, a burst rings once, WAKE rings
through everything, and a bell that cannot read its letter still rings.
v0.17: bells racing on one second take turns, and a bell that had to ring
without its policy says so."""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "listeners" / "on-deliver" / "chime-policy.py"
CHIME_SH = ROOT / "listeners" / "on-deliver" / "chime.sh"
spec = importlib.util.spec_from_file_location("chime_policy", POLICY)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


def _env(**kw):
    return {"DISCOBUS_CHIME_SKIP_FROM": "feed-bot,cron-bot", "DISCOBUS_CHIME_COOLDOWN": "90", **kw}


def _proc_env(state: str, **kw) -> dict:
    # a full os.environ underneath: Windows needs SystemRoot to start a process
    return {**os.environ, **_env(**kw), "DISCOBUS_CHIME_STATE": state}


def _run_policy(stdin: str, env: dict, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(POLICY)], input=stdin, capture_output=True, text=True, env=env, **kw)


def _letter(sender: str, subject: str) -> str:
    return json.dumps({"from": sender, "subject": subject})


def test_agents_ring_and_robots_do_not(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(), {"from": "beta", "subject": "re-12 done"}, 1000.0, st) == (True, "ring", "")
    ring, why, _ = cp.decide(_env(), {"from": "feed-bot", "subject": "daily digest"}, 2000.0, st)
    assert not ring and "skip list" in why


def test_skip_list_is_case_insensitive_like_the_rest_of_the_bus(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(DISCOBUS_CHIME_SKIP_FROM="Feed-Bot"), {"from": "feed-bot", "subject": "x"}, 1000.0, st)[0] is False
    assert cp.decide(_env(), {"from": "FEED-BOT", "subject": "x"}, 1001.0, st)[0] is False


def test_a_burst_rings_once_then_again_after_the_window(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(), {"from": "beta", "subject": "one"}, 1000.0, st)[0]
    ring, why, _ = cp.decide(_env(), {"from": "gamma", "subject": "two"}, 1030.0, st)
    assert not ring and why.startswith("cooldown")
    assert cp.decide(_env(), {"from": "gamma", "subject": "three"}, 1091.0, st)[0]
    # cooldown off = every letter rings
    assert cp.decide(_env(DISCOBUS_CHIME_COOLDOWN="0"), {"from": "beta", "subject": "four"}, 1092.0, st)[0]


def test_a_stamp_slightly_in_the_future_is_the_bell_that_beat_us_to_the_lock(tmp_path):
    st = str(tmp_path / "last")
    cp.write_last_ring(st, 1005.0)
    assert cp.decide(_env(), {"from": "beta", "subject": "x"}, 1000.0, st)[0] is False   # 5 s "ahead": inside the window
    cp.write_last_ring(st, 1_000_000.0)
    assert cp.decide(_env(), {"from": "beta", "subject": "x"}, 1000.0, st)[0] is True    # a bogus far-future stamp: ring


def test_wake_marker_rings_through_skip_list_and_cooldown(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(), {"from": "beta", "subject": "one"}, 1000.0, st)[0]
    ring, why, _ = cp.decide(_env(), {"from": "cron-bot", "subject": "WAKE: job failed"}, 1001.0, st)
    assert ring and "wake" in why
    assert cp.decide(_env(), {"from": "beta", "subject": "urgent-ish"}, 1002.0, st)[0] is False  # lower-case, glued: not a marker
    assert cp.decide(_env(DISCOBUS_CHIME_WAKE_RE="(?i)poke"), {"from": "beta", "subject": "a Poke please"}, 1003.0, st)[0]
    assert cp.decide(_env(DISCOBUS_CHIME_WAKE_RE="(["), {"from": "beta", "subject": "URGENT"}, 1004.0, st)[0]  # bad regex -> default


def test_bad_cooldown_rings_and_complains_out_loud(tmp_path):
    st = str(tmp_path / "last")
    ring, why, complaint = cp.decide(_env(DISCOBUS_CHIME_COOLDOWN="90   # seconds"), {"from": "beta", "subject": "x"}, 1000.0, st)
    assert ring and why == "ring" and "DISCOBUS_CHIME_COOLDOWN" in complaint
    r = _run_policy(_letter("beta", "y"), _proc_env(st, DISCOBUS_CHIME_COOLDOWN="90   # seconds"))
    assert r.returncode == 0 and "chime-policy: bad DISCOBUS_CHIME_COOLDOWN" in r.stderr


def test_unparseable_letter_still_rings(tmp_path):
    st = str(tmp_path / "last")
    assert cp.decide(_env(), None, 1000.0, st) == (True, "unparseable envelope: ring", "")
    # end to end through the script: exit 0 = ring, 3 = silent; a plain ring says nothing on stderr
    env = _proc_env(st)
    assert _run_policy("not json", env).returncode == 0
    assert _run_policy(_letter("feed-bot", "x"), env).returncode == 3
    r = _run_policy(_letter("beta", "x"), env)
    assert (r.returncode, r.stderr) == (0, "")
    r = _run_policy(_letter("beta", "y"), env)
    assert r.returncode == 3 and "cooldown" in r.stderr   # inside the window, reason on stderr
    assert _run_policy(_letter("beta", "WAKE"), env).returncode == 0


def test_state_path_expands_tilde(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir(); cwd.mkdir()
    monkeypatch.setenv("HOME", str(home)); monkeypatch.setenv("USERPROFILE", str(home))
    env = _proc_env("~/last")
    assert _run_policy(_letter("beta", "x"), env, cwd=str(cwd)).returncode == 0
    assert _run_policy(_letter("beta", "y"), env, cwd=str(cwd)).returncode == 3   # same state file, so silent
    assert (home / "last").exists() and not (cwd / "~").exists()


def test_eight_concurrent_bells_ring_exactly_once(tmp_path):
    st = str(tmp_path / "last")
    env = _proc_env(st)
    procs = [subprocess.Popen([sys.executable, str(POLICY)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, text=True, env=env) for _ in range(8)]
    for i, p in enumerate(procs):      # hand every bell its letter BEFORE waiting on any: they run together
        p.stdin.write(_letter(f"agent{i}", "burst")); p.stdin.close()
    rcs = sorted(p.wait() for p in procs)
    assert rcs == [0] + [3] * 7, rcs
    assert not list(tmp_path.glob("last.*.tmp"))   # no half-written stamps left behind


@pytest.mark.skipif(os.name == "nt", reason="bash wrapper; shell=True is cmd.exe on Windows")
def test_chime_sh_passes_silence_drains_without_python_and_complains_without_policy(tmp_path):
    st = str(tmp_path / "last")
    bash = shutil.which("bash")
    # a PATH with only what the wrapper needs, and no player: the test stays silent
    tools = tmp_path / "bin"; tools.mkdir()
    for name, target in (("dirname", shutil.which("dirname")), ("cat", shutil.which("cat")), ("python3", sys.executable)):
        (tools / name).symlink_to(target)
    env = {**_proc_env(st), "PATH": str(tools), "DISCOBUS_CHIME_SOUND": str(tmp_path / "nope.ogg")}
    def run(script: Path, stdin: str, e: dict) -> subprocess.CompletedProcess:
        return subprocess.run([bash, str(script)], input=stdin, capture_output=True, text=True, env=e)
    r = run(CHIME_SH, _letter("feed-bot", "x"), env)
    assert r.returncode == 3 and "skip list" in r.stderr
    r = run(CHIME_SH, _letter("beta", "x"), env)
    assert (r.returncode, r.stderr) == (0, "")
    # the wrapper copied WITHOUT its sibling policy: rings, and says why
    lonely = tmp_path / "lonely"; lonely.mkdir()
    shutil.copy(CHIME_SH, lonely / "chime.sh")
    r = run(lonely / "chime.sh", _letter("feed-bot", "x"), env)
    assert r.returncode == 0 and "rang without policy" in r.stderr
    # no python3 at all = v0.15: drain stdin, ring, quiet
    (tools / "python3").unlink()
    r = run(CHIME_SH, _letter("feed-bot", "x"), env)
    assert (r.returncode, r.stderr) == (0, "")


def _reload_listener(monkeypatch, on_deliver: str):
    sys.path.insert(0, str(ROOT / "listeners"))
    monkeypatch.setenv("DISCOBUS_ON_DELIVER", on_deliver)
    return importlib.reload(importlib.import_module("listener"))


def test_listener_logs_silence_as_info_not_warning(tmp_path, caplog, monkeypatch):
    listener = _reload_listener(monkeypatch, "exit 3")
    with caplog.at_level(logging.INFO):
        listener.run_on_deliver({"from": "beta", "subject": "x"})
    assert any("silent by policy" in r.message and r.levelno == logging.INFO for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_listener_warns_when_the_bell_rang_with_a_complaint(tmp_path, caplog, monkeypatch):
    listener = _reload_listener(monkeypatch, "echo policy-missing 1>&2")   # sh and cmd.exe both
    with caplog.at_level(logging.INFO):
        listener.run_on_deliver({"from": "beta", "subject": "x"})
    assert any("rang with a complaint" in r.message and "policy-missing" in r.message and r.levelno == logging.WARNING
               for r in caplog.records)
