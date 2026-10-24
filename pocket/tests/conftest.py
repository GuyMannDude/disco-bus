import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import pocket_bus as pb  # noqa: E402
from stub_dispatcher import StubDispatcher  # noqa: E402

LOGIN = "owner@example.com"
TOKEN = "pb-test-pixel-token"
OTHER_TOKEN = "pb-test-tablet-token"
DOORS = {
    "CC": {"label": "CC", "host": "laptop", "wakes": False, "how": "Claude app, Remote Control"},
    "Opie": {"label": "Opie", "host": "cloud", "wakes": False, "how": "Claude app, your chat"},
    "CC3": {"label": "Linus", "host": "laptop", "wakes": False, "how": "Ask CC: 'tell Linus …'"},
    "CC4": {"label": "Archie", "host": "latitude", "wakes": False, "how": "Letter waits until someone types bus"},
}


def write_private(path: Path, obj) -> None:
    path.write_text(json.dumps(obj))
    os.chmod(path, 0o600)


class Pocket:
    """A running Pocket Bus on an ephemeral loopback port, against a stub."""

    def __init__(self, tmp: Path, stub: StubDispatcher, **cfg):
        self.stub = stub
        self.config_dir = tmp / "config"
        self.config_dir.mkdir(mode=0o700)
        write_private(self.config_dir / "tokens.json",
                      {"pixel": pb.hash_token(TOKEN), "tablet": pb.hash_token(OTHER_TOKEN)})
        (self.config_dir / "doors.json").write_text(json.dumps(DOORS))
        self.cfg = pb.Config(dispatcher=stub.url, login=LOGIN, config_dir=self.config_dir,
                             state_dir=tmp / "state", wake_policy=tmp / "wake-policy.json",
                             host_label="testhost", **cfg)
        self.server = pb.make_server(self.cfg, port=0)
        self.app = self.server.app
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def raw(self, method, path, body=None, headers=None, data=None):
        if body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=10) as r:
                raw = r.read()
                return r.status, _j(raw), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, _j(raw), e.headers

    def api(self, method, route, body=None, token=TOKEN, login=LOGIN, headers=None):
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        if login is not None:
            h["Tailscale-User-Login"] = login
        return self.raw(method, "/api/" + route, body=body, headers=h)

    def audit_lines(self):
        p = self.cfg.audit_file
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _j(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw


@pytest.fixture
def stub():
    s = StubDispatcher()
    yield s
    s.close()


@pytest.fixture
def pocket(tmp_path, stub):
    p = Pocket(tmp_path, stub)
    yield p
    p.close()
