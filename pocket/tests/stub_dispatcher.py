"""A small in-memory dispatcher with the real one's routes and answers, for
Pocket Bus tests. It records every call so tests can assert what Pocket Bus
asked the bus to do (and what it never asked)."""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubDispatcher:
    def __init__(self, registry=("Guy", "CC", "Opie", "CC3", "sec-watch")):
        self.registry = set(registry)
        self.msgs: list[dict] = []
        self.pauses: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.lock = threading.Lock()
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _out(self, status, payload, headers=None):
                b = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                for k, v in (headers or {}).items():
                    self.send_header(k, str(v))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                p = urllib.parse.urlsplit(self.path)
                qs = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
                stub.calls.append(("GET", p.path, qs, None))
                self._out(*stub.get(p.path, qs))

            def do_POST(self):
                p = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.calls.append(("POST", p.path, {}, body))
                self._out(*stub.post(p.path, body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- seeding ------------------------------------------------------------
    def add(self, frm, to, subject="s", body=None, reply_to=None, state="DELIVERED",
            read_at=None, created_at="2026-10-23T20:00:00Z") -> int:
        with self.lock:
            mid = len(self.msgs) + 1
            self.msgs.append({"mesh_version": "0.5", "id": mid, "tracking_id": f"msg-{mid}",
                              "from": frm, "to": to, "reply_to": reply_to, "subject": subject,
                              "body": body if body is not None else {"text": subject},
                              "state": state, "created_at": created_at, "delivered_at": None,
                              "delivery_error": None, "read_at": read_at, "cleared_at": None,
                              "cleared_reason": None, "archived_at": None, "archive_reason": None})
            return mid

    def by_id(self, mid):
        return next((m for m in self.msgs if m["id"] == mid), None)

    def paths(self, method=None):
        return [c[1] for c in self.calls if method is None or c[0] == method]

    # -- routes (shapes copied from dispatcher.py) -----------------------------
    def get(self, path, qs):
        if path == "/healthz":
            return 200, {"status": "ok", "mesh_version": "0.5"}
        if path.startswith("/mesh/state/"):
            m = self.by_id(int(path.rsplit("/", 1)[1]))
            return (200, m) if m else (404, {"error": "not found"})
        if path == "/mesh/history":
            lim = int(qs.get("limit", 50))
            return 200, list(reversed(self.msgs))[:lim]
        if path == "/mesh/search":
            rows = list(reversed(self.msgs))
            for f in ("from", "to"):
                if qs.get(f):
                    if qs[f] not in self.registry:
                        return 404, {"error": f"unknown agent: {qs[f]}"}
                    rows = [m for m in rows if m[f] == qs[f]]
            if qs.get("q"):
                rows = [m for m in rows if qs["q"] in m["subject"] or qs["q"] in json.dumps(m["body"])]
            lim = int(qs.get("limit", 50))
            return 200, rows[:lim], {"X-Search-Total": len(rows)}
        if path.startswith("/mesh/inbox/"):
            agent = path.rsplit("/", 1)[1]
            if agent not in self.registry:
                return 404, {"error": f"unknown agent: {agent}"}
            rows = [m for m in reversed(self.msgs) if m["to"] == agent and m["state"] not in ("HELD", "DROPPED")]
            if qs.get("filter") == "unread":
                rows = [m for m in rows if m["read_at"] is None]
            lim = int(qs.get("limit", 50))
            return 200, rows[:lim], {"X-Inbox-Total": len(rows)}
        if path.startswith("/mesh/thread/"):
            mid = int(path.rsplit("/", 1)[1])
            m = self.by_id(mid)
            if not m:
                return 404, {"error": f"message {mid} not found"}
            while m["reply_to"]:
                m = self.by_id(m["reply_to"])
            ids, grew = {m["id"]}, True
            while grew:
                more = {x["id"] for x in self.msgs if x["reply_to"] in ids} - ids
                grew = bool(more)
                ids |= more
            return 200, {"root_id": m["id"], "messages": [x for x in self.msgs if x["id"] in ids]}
        if path == "/mesh/pause":
            held = {}
            for m in self.msgs:
                if m["state"] == "HELD":
                    held[m["from"]] = held.get(m["from"], 0) + 1
            return 200, {"paused": [{"agent": a, "paused_at": t, "held": held.get(a, 0)}
                                    for a, t in sorted(self.pauses.items())],
                         "held_orphans": [{"agent": a, "held": n} for a, n in held.items() if a not in self.pauses]}
        return 404, {"error": "not found"}

    def post(self, path, body):
        if path == "/mesh/ping":
            for f in ("from", "to"):
                if body.get(f) not in self.registry:
                    return 400, {"error": f"unknown '{f}' agent: {body.get(f)}"}
            state = "HELD" if body["from"] in self.pauses else "SENT"
            mid = self.add(body["from"], body["to"], body["subject"], body["body"],
                           body.get("reply_to"), state=state)
            return 202, {"id": mid, "tracking_id": f"msg-{mid}", "state": state}
        if path.startswith("/mesh/read/"):
            m = self.by_id(int(path.rsplit("/", 1)[1]))
            if not m:
                return 404, {"error": "not found"}
            if m["to"] != body.get("agent"):
                return 403, {"error": "only the recipient can mark this message read"}
            first = m["read_at"] is None
            m["read_at"] = m["read_at"] or "2026-10-23T21:00:00Z"
            return 200, {**m, "first_read": first}
        if path in ("/mesh/pause", "/mesh/play"):
            agent = body.get("agent")
            if agent not in self.registry:
                return 400, {"error": f"unknown agent: {agent}"}
            if path == "/mesh/pause":
                self.pauses.setdefault(agent, "2026-10-23T21:30:00Z")
                held = sum(1 for m in self.msgs if m["state"] == "HELD" and m["from"] == agent)
                return 200, {"agent": agent, "paused": True, "paused_at": self.pauses[agent], "held": held}
            self.pauses.pop(agent, None)
            released = []
            for m in self.msgs:
                if m["state"] == "HELD" and m["from"] == agent:
                    m["state"] = "DELIVERED"
                    released.append({"id": m["id"]})
            return 200, {"agent": agent, "paused": False, "released": released}
        return 404, {"error": "not found"}
