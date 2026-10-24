"""Pocket Bus against the REAL dispatcher (dispatcher/dispatcher.py, temp DB,
temp registry, Discord mirror stubbed): the whole loop, including the
dispatcher delivering a letter to Pocket Bus's own /inbox."""
import importlib.util
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import pocket_bus as pb
from conftest import Pocket

DISPATCHER = Path(__file__).resolve().parents[2] / "dispatcher" / "dispatcher.py"
pytest.importorskip("requests")


def _load_dispatcher():
    sys.path.insert(0, str(DISPATCHER.parent))
    spec = importlib.util.spec_from_file_location("pocket_test_dispatcher", DISPATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AgentListener:
    """A stand-in agent listener that records what the dispatcher delivers."""

    def __init__(self):
        self.got = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/inbox"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class Bus:
    def __init__(self, tmp: Path, monkeypatch):
        self.d = _load_dispatcher()
        # Never reach the live Discord from a test (scoped to this test only).
        monkeypatch.setattr(self.d.discord_mirror, "mirror", lambda env: None)
        monkeypatch.setattr(self.d.discord_mirror, "post_ack", lambda env, subj: None)
        self.d.DB_PATH = tmp / "bus.sqlite"
        self.d.AGENTS_PATH = tmp / "agents.json"
        self.d.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.d.DispatcherHandler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.agents = AgentListener()

    def register(self, guy_url: str):
        self.d.AGENTS_PATH.write_text(json.dumps({
            "Guy": {"url": guy_url, "human": True},
            "CC": {"url": self.agents.url}, "Opie": {"url": self.agents.url},
            "sec-watch": {"url": "http://127.0.0.1:9/inbox"},
        }))

    def ping(self, frm, to, text, reply_to=None):
        env = {"mesh_version": "0.5", "from": frm, "to": to, "subject": text[:30], "body": {"text": text}}
        if reply_to:
            env["reply_to"] = reply_to
        req = urllib.request.Request(self.url + "/mesh/ping", data=json.dumps(env).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def state(self, mid):
        with urllib.request.urlopen(f"{self.url}/mesh/state/{mid}", timeout=5) as r:
            return json.loads(r.read())

    def settled(self, mid, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            env = self.state(mid)
            if env["state"] not in ("SENT",):
                return env
            time.sleep(0.05)
        return self.state(mid)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.agents.server.shutdown()
        self.agents.server.server_close()


class _Stubless:
    """Pocket's conftest helper wants a stub with .url; the real bus has one."""

    def __init__(self, url):
        self.url = url


@pytest.fixture
def loop(tmp_path, monkeypatch):
    bus = Bus(tmp_path, monkeypatch)
    pocket = Pocket(tmp_path, _Stubless(bus.url))
    bus.register(pocket.base + "/inbox")
    yield bus, pocket
    pocket.close()
    bus.close()


def test_a_letter_from_the_phone_lands_as_guy(loop):
    bus, pocket = loop
    status, data, _ = pocket.api("POST", "send", {"to": ["CC"], "text": "testing from my phone", "from": "CC"})
    assert status == 200 and data["ok"], data
    env = bus.settled(data["receipts"][0]["id"])
    assert env["from"] == "Guy" and env["state"] == "DELIVERED"
    assert env["body"] == {"text": "testing from my phone", "via": "pocket"}
    assert env["subject"] == "testing-from-my-phone"
    assert bus.agents.got[-1]["from"] == "Guy"


def test_a_letter_to_guy_is_delivered_to_the_pocket_listener(loop):
    bus, pocket = loop
    mid = bus.ping("CC", "Guy", "your amp is ready")["id"]
    assert bus.settled(mid)["state"] == "DELIVERED"          # the dispatcher reached /inbox: 200
    assert pocket.app.listener["received"] == 1 and pocket.app.listener["last_id"] == mid
    _, inbox, _ = pocket.api("GET", "inbox?filter=unread")
    assert [(m["id"], m["outside_door"]) for m in inbox["letters"]] == [(mid, False)]


def test_reply_thread_read_and_tag_through_the_real_bus(loop):
    bus, pocket = loop
    _, sent, _ = pocket.api("POST", "send", {"to": ["Opie"], "text": "what's for supper"})
    ask = sent["receipts"][0]["id"]
    answer = bus.ping("Opie", "Guy", "soup", reply_to=ask)["id"]
    cold = bus.ping("Opie", "Guy", "unrelated news")["id"]
    bus.settled(answer)
    bus.settled(cold)
    _, thread, _ = pocket.api("GET", f"thread/{answer}")
    assert [(m["id"], m["mine"]) for m in thread["messages"]] == [(ask, True), (answer, False)]
    _, inbox, _ = pocket.api("GET", "inbox")
    assert {m["id"]: m["outside_door"] for m in inbox["letters"]} == {answer: False, cold: True}
    assert pocket.api("POST", f"read/{answer}", {})[0] == 200
    assert bus.state(answer)["read_at"]
    assert pocket.api("POST", f"read/{ask}", {})[0] == 403        # Guy's own letter, to Opie


def test_pause_and_play_through_the_real_bus(loop):
    bus, pocket = loop
    assert pocket.api("POST", "pause", {"agent": "CC"})[1]["paused"] is True
    held = bus.ping("CC", "Guy", "while paused")
    assert held["state"] == "HELD"
    _, holds, _ = pocket.api("GET", "holds")
    assert {a["agent"]: (a["paused"], a["held"]) for a in holds["agents"]}["CC"] == (True, 1)
    _, played, _ = pocket.api("POST", "play", {"agent": "CC"})
    assert played["released"] == 1
    assert bus.settled(held["id"])["state"] == "DELIVERED"


def test_fleet_and_search_through_the_real_bus(loop):
    bus, pocket = loop
    mid = bus.ping("CC", "Guy", "the tube amp is humming")["id"]
    bus.settled(mid)
    _, fleet, _ = pocket.api("GET", "fleet")
    cards = {c["agent"]: c for c in fleet["agents"]}
    assert cards["CC"]["unread"] == 1 and cards["CC"]["last_heard"]
    assert cards["CC3"]["on_bus"] is False                        # in doors, not in this registry
    _, found, _ = pocket.api("GET", "search?q=tube")
    assert [m["id"] for m in found["letters"]] == [mid] and found["total"] == 1
    _, health, _ = pocket.api("GET", "health")
    assert health["ok"] is True and health["registered"] is True and health["unread"] == 1


def test_a_reply_to_a_sink_cannot_be_sent_from_the_phone(loop):
    bus, pocket = loop
    sink_letter = bus.ping("sec-watch", "Guy", "digest")["id"]
    status, _, _ = pocket.api("POST", "send", {"to": ["sec-watch"], "text": "ack", "reply_to": sink_letter})
    assert status == 400
    assert bus.state(sink_letter)["cleared_reason"] is None        # never acked


def test_the_pocket_module_never_imports_requests():
    src = Path(pb.__file__).read_text()
    assert "import requests" not in src                            # stdlib only
