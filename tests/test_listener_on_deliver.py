"""v0.15 on-deliver bell: a letter rings once, a status light does not, and a
broken bell never loses a letter. Runs the real listener on a free port."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
LISTENER = ROOT / "listeners" / "listener.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def listener(tmp_path):
    port = _free_port()
    marker = tmp_path / "rang.jsonl"
    bell = tmp_path / "bell.sh"
    # the bell appends whatever arrived on stdin: proves "once" and "envelope on stdin"
    bell.write_text(f"#!/usr/bin/env bash\ncat >> {marker}\necho >> {marker}\n")
    bell.chmod(0o755)
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"alpha": {}}))
    env = {
        **os.environ,
        "DISCOBUS_AGENT": "alpha",
        "DISCOBUS_PORT": str(port),
        "DISCOBUS_AGENTS_FILE": str(agents),
        "DISCOBUS_INBOX": str(tmp_path / "inbox"),
        "DISCOBUS_ON_DELIVER": str(bell),
        "DISCOBUS_ON_DELIVER_TIMEOUT": "5",
    }
    proc = subprocess.Popen([sys.executable, str(LISTENER)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                if requests.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            proc.kill()
            raise RuntimeError("listener did not come up: " + (proc.stdout.read() if proc.stdout else ""))
        yield port, marker, tmp_path / "inbox" / "alpha", bell
    finally:
        proc.kill()
        proc.wait(timeout=5)


def _env(msg_id: int, **extra) -> dict:
    return {"mesh_version": "0.5", "id": msg_id, "tracking_id": f"msg-{msg_id}-t",
            "from": "beta", "to": "alpha", "subject": f"hello {msg_id}", "body": {"k": "v"}, **extra}


def _wait(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


@pytest.mark.skipif(os.name == "nt", reason="bell fixture is a bash script; shell=True is cmd.exe on Windows, which cannot run it (CC2 #3548)")
def test_letter_rings_once_with_envelope_on_stdin(listener):
    port, marker, inbox, _ = listener
    r = requests.post(f"http://127.0.0.1:{port}/inbox", json=_env(1), timeout=5)
    assert r.status_code == 200 and r.json()["state"] == "DELIVERED"
    assert _wait(lambda: marker.exists() and marker.read_text().strip())
    assert _wait(lambda: (inbox / "msg-1-t.json").exists())
    time.sleep(0.3)   # a second ring would land here
    rings = [json.loads(l) for l in marker.read_text().splitlines() if l.strip()]
    assert len(rings) == 1 and rings[0]["id"] == 1 and rings[0]["subject"] == "hello 1"


def test_status_furniture_does_not_ring(listener):
    port, marker, inbox, _ = listener
    r = requests.post(f"http://127.0.0.1:{port}/inbox", json=_env(2, **{"class": "status"}), timeout=5)
    assert r.status_code == 200
    assert _wait(lambda: (inbox / "msg-2-t.json").exists())   # still written
    time.sleep(0.5)
    assert not marker.exists()                                  # but silent


def test_broken_bell_never_loses_a_letter(listener):
    port, marker, inbox, bell = listener
    bell.write_text("#!/usr/bin/env bash\nexit 3\n")
    r = requests.post(f"http://127.0.0.1:{port}/inbox", json=_env(3), timeout=5)
    assert r.status_code == 200 and r.json()["state"] == "DELIVERED"
    assert _wait(lambda: (inbox / "msg-3-t.json").exists())
