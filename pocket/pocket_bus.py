#!/usr/bin/env python3
"""
Pocket Bus — a phone face for the bus, and one human's mailbox on it.

One small service, three jobs:
  /app/*    a static phone web app (closed allowlist, secret-free)
  /api/*    token-checked JSON for that app; every dispatcher call is made
            as the human's bus identity (POCKET_AGENT, default "Guy"), never
            a client-supplied one
  /inbox    the human's LISTENER: the dispatcher POSTs letters here like it
            does to any agent, so mail to the human is DELIVERED, not swept

The dispatcher stays dumb and stays the store: Pocket Bus writes no mail to
disk. Its only files are its own config (tokens, doors, kill switch) and an
audit log of writes.

Security (spec-pocket-bus-v0 §5):
  S1  binds 127.0.0.1 only. The phone reaches it through Tailscale Serve,
      which adds Tailscale-User-Login and strips client-supplied copies.
  S2  the per-device bearer token is THE gate. tokens.json (0600) holds only
      SHA-256 hashes, one named line per device; delete a line = that device
      is out on its next request. The Tailscale-User-Login check is
      defense-in-depth only: when every tailnet node signs in as the same
      person it cannot tell one of their devices from another. It does shut
      out tagged devices, users a node is shared with, and Funnel traffic
      (no header).
  S3  the sender is server-bound: `from` is always POCKET_AGENT. Letters go
      only to agents in doors.json, so no route reaches a sink ack while the
      doors file lists no sink; none reaches release or drop at all.
  S4  static files are an allowlist and contain nothing private.
  S5  the app renders letters with textContent only; strict CSP on every
      response (default-src 'self', no inline script).
  S6  one JSONL audit line per write (no bodies), rotated at 10 MB, never
      deleted.
  S7  30 letters per 10 minutes per token.
  S8  the file DISABLED in the config dir switches every /api route to 503;
      /inbox keeps accepting so no mail is lost.

Configuration (env):
  POCKET_DISCOBUS_URL     dispatcher base URL                    (required)
  POCKET_TAILSCALE_LOGIN  the one Tailscale login allowed in     (required)
  POCKET_AGENT            bus identity this service speaks as    (default Guy)
  POCKET_DOOR_AGENT       the one agent allowed to write first   (default CC)
  POCKET_HOST_LABEL       machine name shown in the header       (default "the bus host")
  POCKET_PORT             loopback port                          (default 9101)
  POCKET_CONFIG_DIR       tokens.json, doors.json, DISABLED      (default ~/.config/pocket-bus)
  POCKET_STATE_DIR        audit.jsonl                            (default ~/.local/state/pocket-bus)
  POCKET_WAKE_POLICY      wake-policy.json; when present it wins over doors.json
                          for `wakes`                            (default <repo>/wake/wake-policy.json)

Usage:
  pocket_bus.py              serve
  pocket_bus.py mint NAME    create a device token, store its hash, print it once
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MESH_VERSION = "0.5"
BIND_HOST = "127.0.0.1"          # S1: never configurable
STATIC_DIR = Path(__file__).resolve().parent / "static"
REPO_ROOT = Path(__file__).resolve().parent.parent

SEND_TEXT_MAX_BYTES = 16 * 1024          # a phone letter doesn't need more
API_BODY_MAX_BYTES = 256 * 1024          # 16 KB of text, JSON-escaped, fits
# The dispatcher caps a letter BODY at 1 MiB but not the envelope around it,
# so the listener allows headroom for any normal letter. (A letter with a
# subject of megabytes would still be refused here and end up FAILED.)
INBOX_MAX_BYTES = 2 * 1024 * 1024
DISPATCHER_READ_MAX = 16 * 1024 * 1024   # a bigger answer from the dispatcher is an error, not a read
FLATTEN_MAX_DEPTH = 20                   # deeper nesting shows as "…" (a crafted body can't recurse us out)
UNREAD_COUNT_PAGE = 200                  # Fleet's per-sender counts cover this many unread letters
SUBJECT_MAX = 200
DERIVED_SUBJECT_MAX = 60
SEND_LIMIT, SEND_WINDOW_SEC = 30, 600    # S7
AUDIT_MAX_BYTES = 10 * 1024 * 1024       # S6
LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX = 50, 200
PREVIEW_CHARS = 240
DISPATCHER_TIMEOUT_SEC = 8
TOKEN_MAX_LEN = 512
ID_MAX_DIGITS = 18
EXIT_CONFIG = 78     # EX_CONFIG: the unit file's RestartPreventExitStatus stops a restart loop on it

# Headers Tailscale Serve (or any reverse proxy) adds. The dispatcher talks to
# /inbox directly over loopback and never sends them, so their presence means
# the request came through the proxy, i.e. from a phone, not the bus.
PROXY_HEADERS = ("Tailscale-User-Login", "Tailscale-User-Name", "X-Forwarded-For",
                 "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")

STATIC_FILES = {
    "/app/": ("index.html", "text/html; charset=utf-8"),
    "/app/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/app/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/app/icon.svg": ("icon.svg", "image/svg+xml"),
    "/app/icon-192.png": ("icon-192.png", "image/png"),
    "/app/icon-512.png": ("icon-512.png", "image/png"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [pocket-bus] %(message)s")
log = logging.getLogger("pocket-bus")


class ConfigError(Exception):
    """A required setting is missing or malformed. The service refuses to start."""


class DispatcherDown(Exception):
    """The dispatcher could not be reached at all (as opposed to answering an error)."""


class TokenFileError(Exception):
    """tokens.json exists but cannot be trusted (loose permissions, bad JSON)."""


@dataclass(frozen=True)
class Config:
    dispatcher: str
    login: str
    agent: str = "Guy"
    door_agent: str = "CC"
    host_label: str = "the bus host"
    port: int = 9101
    config_dir: Path = Path.home() / ".config" / "pocket-bus"
    state_dir: Path = Path.home() / ".local" / "state" / "pocket-bus"
    wake_policy: Path = REPO_ROOT / "wake" / "wake-policy.json"

    @property
    def tokens_file(self) -> Path:
        return self.config_dir / "tokens.json"

    @property
    def doors_file(self) -> Path:
        return self.config_dir / "doors.json"

    @property
    def kill_file(self) -> Path:
        return self.config_dir / "DISABLED"

    @property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.jsonl"


def config_from_env(env=os.environ) -> Config:
    url = env.get("POCKET_DISCOBUS_URL", "").strip().rstrip("/")
    if not url:
        raise ConfigError("POCKET_DISCOBUS_URL is not set (the dispatcher's address, e.g. http://<host>:9100)")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"POCKET_DISCOBUS_URL must be an http(s) URL, got {url!r}")
    login = env.get("POCKET_TAILSCALE_LOGIN", "").strip()
    if not login:
        raise ConfigError("POCKET_TAILSCALE_LOGIN is not set (the one Tailscale login allowed in)")
    for key, value in (("POCKET_DISCOBUS_URL", url), ("POCKET_TAILSCALE_LOGIN", login)):
        if "<" in value or ">" in value:
            raise ConfigError(f"{key} is still the env.example placeholder: {value!r}")
    try:
        port = int(env.get("POCKET_PORT", "9101"))
    except ValueError:
        raise ConfigError("POCKET_PORT must be a number") from None
    kw = {}
    for key, field in (("POCKET_CONFIG_DIR", "config_dir"), ("POCKET_STATE_DIR", "state_dir"),
                       ("POCKET_WAKE_POLICY", "wake_policy")):
        if env.get(key):
            kw[field] = Path(env[key]).expanduser()
    return Config(dispatcher=url, login=login, port=port,
                  agent=env.get("POCKET_AGENT", "Guy").strip() or "Guy",
                  door_agent=env.get("POCKET_DOOR_AGENT", "CC").strip() or "CC",
                  host_label=env.get("POCKET_HOST_LABEL", "").strip() or "the bus host", **kw)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- letters --

def derive_subject(text: str, agent: str = "Guy") -> str:
    """The bus needs a subject; a phone letter rarely has one. Kebab-case the
    first words of the first line, whole words only, at most 60 chars."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    words = re.findall(r"[^\W_]+", first.lower())
    out = ""
    for w in words:
        candidate = f"{out}-{w}" if out else w
        if len(candidate) > DERIVED_SUBJECT_MAX:
            break
        out = candidate
    if not out and words:                      # one giant first word
        out = words[0][:DERIVED_SUBJECT_MAX]
    return out or f"note-from-{agent.lower()}"


def _scalar(v) -> str:
    if v is None or v == {} or v == []:
        return "(none)"
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v)


def _flatten(value, depth: int) -> list[str]:
    pad, lines = "  " * depth, []
    if depth >= FLATTEN_MAX_DEPTH:
        return [f"{pad}…"]
    items = value.items() if isinstance(value, dict) else ((None, v) for v in value)
    for k, v in items:
        label = f"{pad}{str(k).replace('_', ' ')}:" if k is not None else f"{pad}-"
        if isinstance(v, (dict, list)) and v:
            lines.append(label)
            lines.extend(_flatten(v, depth + 1))
        else:
            lines.append(f"{label} {_scalar(v)}")
    return lines


def display_text(body) -> str:
    """What a human reads: body.text when there is one, otherwise the body
    flattened to `key: value` lines. Never raw JSON (that is under Details)."""
    if isinstance(body, dict):
        text = body.get("text")
        if isinstance(text, str) and text.strip():
            return text
        return "\n".join(_flatten(body, 0)) or "(empty letter)"
    if isinstance(body, list):
        return "\n".join(_flatten(body, 0)) or "(empty letter)"
    return _scalar(body)


def slim(env: dict, *, full: bool = False) -> dict:
    text = display_text(env.get("body"))
    out = {k: env.get(k) for k in ("id", "from", "to", "subject", "created_at",
                                   "read_at", "reply_to", "state")}
    out["preview"] = " ".join(text.split())[:PREVIEW_CHARS]
    if full:
        out["text"] = text
        out["body"] = env.get("body")
    return out


# -------------------------------------------------------------------- app --

class RateLimit:
    """Sliding window per key. reserve() checks and records atomically, so
    two concurrent sends cannot both squeeze under the line."""

    def __init__(self, limit: int = SEND_LIMIT, window: float = SEND_WINDOW_SEC):
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def reserve(self, key: str, n: int) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) + n > self.limit:
                self._hits[key] = hits
                return False
            self._hits[key] = hits + [now] * n
            return True


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started = time.time()
        self.limiter = RateLimit()
        self.audit_max = AUDIT_MAX_BYTES
        self._audit_lock = threading.Lock()
        self._listener_lock = threading.Lock()
        self.listener = {"received": 0, "last_at": None, "last_id": None}
        self._parents: dict[int, tuple[str, str] | None] = {}
        # Never route the dispatcher through an HTTP proxy from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # -- dispatcher -------------------------------------------------------
    def dispatch(self, method: str, path: str, query: dict | None = None, body=None):
        url = self.cfg.dispatcher + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=DISPATCHER_TIMEOUT_SEC) as r:
                return r.status, _json_or_none(_read_capped(r)), r.headers
        except urllib.error.HTTPError as e:
            return e.code, _json_or_none(_read_capped(e)), e.headers
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DispatcherDown(str(getattr(e, "reason", e))) from None

    # -- auth ---------------------------------------------------------------
    def disabled(self) -> bool:
        return self.cfg.kill_file.exists()

    def token_name(self, authorization: str) -> str | None:
        """Name of the device whose token this is, or None. Re-read per
        request: deleting a line in tokens.json takes effect immediately."""
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token or len(token) > TOKEN_MAX_LEN:
            return None
        path = self.cfg.tokens_file
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if st.st_mode & 0o077:
            raise TokenFileError(f"{path} is readable by others; chmod 600 it")
        try:
            table = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise TokenFileError(f"{path} is unreadable: {e}") from None
        if not isinstance(table, dict):
            raise TokenFileError(f"{path} must be a JSON object of name -> sha256")
        presented = hash_token(token)
        match = None
        for name, digest in table.items():   # compare every line: no early exit
            if isinstance(digest, str) and hmac.compare_digest(presented, digest.lower()):
                match = name
        return match

    def login_ok(self, login: str | None) -> bool:
        """Defense-in-depth only (see S2 in the module docstring)."""
        return bool(login) and login.strip().lower() == self.cfg.login.lower()

    # -- facts ----------------------------------------------------------------
    def doors(self) -> tuple[dict[str, dict], str | None]:
        """doors.json as {agent: {label, host, wakes, how}} in file order, plus
        an error string when the file is missing or unreadable."""
        path = self.cfg.doors_file
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, f"no doors file at {path}"
        except (OSError, ValueError) as e:
            return {}, f"doors file unreadable: {e}"
        if isinstance(raw, dict) and isinstance(raw.get("agents"), dict):
            raw = raw["agents"]
        if not isinstance(raw, dict):
            return {}, "doors file must be a JSON object keyed by agent"
        out = {}
        for agent, row in raw.items():
            if agent == self.cfg.agent or not isinstance(row, dict):
                continue
            out[agent] = {
                "label": str(row.get("label") or agent),
                "host": str(row.get("host") or ""),
                "wakes": row.get("wakes") is True,
                "how": str(row.get("how") or ""),
            }
        return out, None

    def wake_policy(self) -> tuple[dict | None, str | None]:
        """(policy, error). None policy = no policy file: doors.json speaks."""
        path = self.cfg.wake_policy
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError) as e:
            return {}, f"wake policy unreadable: {e}"
        if isinstance(raw, dict) and isinstance(raw.get("agents"), dict):
            raw = raw["agents"]
        return (raw if isinstance(raw, dict) else {}), None

    def door(self, agent: str, row: dict, policy: dict | None, policy_err: str | None) -> dict:
        """One home per fact: when a wake policy exists, it — not the doors
        file — says whether a letter wakes this agent. A claim the policy does
        not back is shown as what the policy says, with a note."""
        door = {"wakes": row["wakes"], "how": row["how"], "source": "doors"}
        if policy is None:
            return door
        entry = policy.get(agent)
        wakes = isinstance(entry, dict) and entry.get("wakes") is True
        door.update(wakes=wakes, source="wake-policy")
        if policy_err:
            door["note"] = "Can't read the wake policy, so no wake is claimed."
        elif row["wakes"] and not wakes:
            door["note"] = "The doors file says a letter wakes this agent; the wake policy says it does not."
        return door

    def parent_parties(self, msg_id: int) -> tuple[str, str] | None:
        """(from, to) of a letter, cached: both are immutable once sent."""
        if msg_id in self._parents:
            return self._parents[msg_id]
        status, data, _ = self.dispatch("GET", f"/mesh/state/{msg_id}")
        if status != 200 or not isinstance(data, dict):
            return None       # not cached: that id may exist later
        parties = (data.get("from"), data.get("to"))
        if len(self._parents) > 5000:
            self._parents.clear()
        self._parents[msg_id] = parties
        return parties

    def outside_door(self, env: dict) -> bool:
        """Single door (doctrine-single-door): only the door agent writes to the
        human first; anyone may answer. A letter is a reply when its parent is
        a letter from or to the human. Anything else is tagged — shown, never
        blocked."""
        if env.get("to") != self.cfg.agent or env.get("from") in (self.cfg.door_agent, self.cfg.agent):
            return False
        parent = env.get("reply_to")
        if not isinstance(parent, int):
            return True
        parties = self.parent_parties(parent)
        return parties is None or self.cfg.agent not in parties

    # -- audit ----------------------------------------------------------------
    def audit(self, **fields) -> None:
        line = json.dumps({"ts": now_iso(), **fields}, ensure_ascii=False)
        path = self.cfg.audit_file
        with self._audit_lock:
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size >= self.audit_max:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    path.rename(path.with_name(f"audit-{stamp}.jsonl"))   # rotated, never deleted
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                log.error("AUDIT WRITE FAILED (%s): %s", e, line)

    def heard(self, env) -> None:
        with self._listener_lock:
            self.listener["received"] += 1
            self.listener["last_at"] = now_iso()
            mid = env.get("id")
            self.listener["last_id"] = mid if isinstance(mid, int) and not isinstance(mid, bool) else None


def _read_capped(resp) -> bytes:
    raw = resp.read(DISPATCHER_READ_MAX + 1)
    if len(raw) > DISPATCHER_READ_MAX:
        raise ApiError(502, "The bus sent back more than Pocket Bus will read.")
    return raw


def _json_or_none(raw: bytes):
    try:
        return json.loads(raw) if raw else None
    except (ValueError, RecursionError):
        return None


def _error_text(data, fallback: str) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return fallback


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def _limit(qs: dict) -> int:
    try:
        n = int(qs.get("limit", [LIST_LIMIT_DEFAULT])[0])
    except ValueError:
        raise ApiError(400, "limit must be a number") from None
    return max(1, min(n, LIST_LIMIT_MAX))


# ---------------------------------------------------------------- handler --

class Handler(BaseHTTPRequestHandler):
    server_version = "PocketBus/0"
    sys_version = ""

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format, *args):
        log.info("%s %s", self.address_string(), format % args)

    # -- responses --------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload, extra=None):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra=extra)

    def _err(self, status: int, message: str, **extra):
        self._json(status, {"error": message, **extra})

    def _body(self, cap: int):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Bad Content-Length.") from None
        if length > cap:
            raise ApiError(413, "That's too big to send.")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            raise ApiError(400, "That wasn't valid JSON.") from None

    # -- routing ------------------------------------------------------------------
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _not_allowed(self):
        self._err(405, "Not allowed.")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _not_allowed

    def _route(self, method: str):
        parsed = urllib.parse.urlsplit(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/inbox":
                return self._listener() if method == "POST" else self._not_allowed()
            if path in ("/", "/app"):
                return self._send(302, b"", "text/plain", extra={"Location": "/app/"})
            if path.startswith("/app/"):
                return self._static(path) if method == "GET" else self._not_allowed()
            if path.startswith("/api/"):
                return self._api(method, path[len("/api/"):], qs)
            return self._err(404, "Not found.")
        except ApiError as e:
            return self._err(e.status, e.message, **e.extra)
        except DispatcherDown as e:
            log.warning("dispatcher unreachable: %s", e)
            return self._err(502, "Can't reach the bus dispatcher.", dispatcher_down=True)
        except TokenFileError as e:
            log.error("%s", e)
            return self._err(500, "Pocket Bus's token file is not safe to use. Ask CC to check it.")
        except Exception:
            # Say so: a dropped connection would read on the phone as "can't reach the bus".
            log.exception("unexpected error on %s %s", method, path)
            return self._err(500, "Something went wrong inside Pocket Bus.", server_error=True)

    def _static(self, path: str):
        entry = STATIC_FILES.get(path)
        if entry is None:
            return self._err(404, "Not found.")
        name, ctype = entry
        try:
            body = (STATIC_DIR / name).read_bytes()
        except OSError:
            return self._err(404, "Not found.")
        extra = {"Service-Worker-Allowed": "/app/"} if name == "sw.js" else None
        self._send(200, body, ctype, cache="no-cache", extra=extra)

    # -- the listener (dispatcher -> human) ---------------------------------------
    def _listener(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # Origin = a web page; a non-JSON type = a no-preflight browser POST.
        if self.client_address[0] not in ("127.0.0.1", "::1") or "Origin" in self.headers \
                or ctype != "application/json" or any(h in self.headers for h in PROXY_HEADERS):
            log.warning("listener refused a non-dispatcher caller %s", self.client_address[0])
            return self._err(403, "The listener only takes letters from the bus.")
        env = self._body(INBOX_MAX_BYTES)
        if not isinstance(env, dict) or env.get("to") != self.app.cfg.agent:
            return self._err(400, f"Not a letter to {self.app.cfg.agent}.")
        self.app.heard(env)
        self._json(200, {"ok": True})

    # -- the app's API ---------------------------------------------------------------
    def _api(self, method: str, route: str, qs: dict):
        app = self.app
        if app.disabled():
            return self._err(503, "Pocket Bus is switched off.", disabled=True)
        device = app.token_name(self.headers.get("Authorization", ""))
        if device is None:
            return self._err(401, "That token isn't valid here.", auth="token")
        if not app.login_ok(self.headers.get("Tailscale-User-Login")):
            log.warning("403 login mismatch for device %s: %r", device,
                        self.headers.get("Tailscale-User-Login"))
            return self._err(403, "This device isn't signed in to Tailscale as the owner.", auth="login")

        if method == "GET":
            if route == "health":
                return self._json(200, self._health())
            if route == "fleet":
                return self._json(200, self._fleet())
            if route == "inbox":
                return self._json(200, self._inbox(qs))
            if route.startswith("thread/"):
                return self._json(200, self._thread(route[len("thread/"):]))
            if route.startswith("agent/"):
                return self._json(200, self._agent(urllib.parse.unquote(route[len("agent/"):])))
            if route == "traffic":
                return self._json(200, self._traffic(qs))
            if route == "search":
                return self._json(200, self._search(qs))
            if route == "holds":
                return self._json(200, self._holds())
        elif method == "POST":
            body = self._body(API_BODY_MAX_BYTES)
            if not isinstance(body, dict):
                raise ApiError(400, "Send a JSON object.")
            if route == "send":
                return self._json(200, self._send_letters(body, device))
            if route.startswith("read/"):
                return self._json(200, self._read(route[len("read/"):], device))
            if route in ("pause", "play"):
                return self._json(200, self._brake(route, body, device))
        return self._err(404, "Not found.")

    # -- GET --------------------------------------------------------------------------
    def _health(self) -> dict:
        app, cfg = self.app, self.app.cfg
        out = {"agent": cfg.agent, "host_label": cfg.host_label,
               "uptime_s": int(time.time() - app.started), "listener": dict(app.listener)}
        try:
            status, data, _ = app.dispatch("GET", "/healthz")
            ok = status == 200 and isinstance(data, dict) and data.get("status") == "ok"
            out["dispatcher"] = {"ok": ok} if ok else {"ok": False, "error": f"dispatcher answered {status}"}
        except DispatcherDown as e:
            out["dispatcher"] = {"ok": False, "error": str(e)}
        if out["dispatcher"]["ok"]:
            try:
                status, _, headers = app.dispatch("GET", f"/mesh/inbox/{cfg.agent}",
                                                  {"filter": "unread", "limit": 1})
            except DispatcherDown as e:
                status, headers = 0, {}
                out["dispatcher"] = {"ok": False, "error": str(e)}
            out["registered"] = status == 200
            if status == 200:
                out["unread"] = _int_header(headers, "X-Inbox-Total", 0)
        out["ok"] = out["dispatcher"]["ok"] and out.get("registered", False)
        _, doors_err = app.doors()
        if doors_err:
            out["doors_error"] = doors_err
        return out

    def _unread(self) -> tuple[dict[str, int] | None, bool]:
        """Unread letters per sender, and whether that covers only the newest
        UNREAD_COUNT_PAGE (said out loud, never silently capped)."""
        status, data, headers = self.app.dispatch(
            "GET", f"/mesh/inbox/{self.app.cfg.agent}", {"filter": "unread", "limit": UNREAD_COUNT_PAGE})
        if status == 404:
            return None, False
        if status != 200 or not isinstance(data, list):
            raise ApiError(502, _error_text(data, f"The bus answered {status}."))
        counts: dict[str, int] = {}
        for env in data:
            counts[env.get("from")] = counts.get(env.get("from"), 0) + 1
        return counts, _int_header(headers, "X-Inbox-Total", len(data)) > len(data)

    def _fleet(self) -> dict:
        app = self.app
        doors, doors_err = app.doors()
        policy, policy_err = app.wake_policy()
        unread, partial = self._unread()
        cards = []
        for agent, row in doors.items():
            status, data, _ = app.dispatch("GET", "/mesh/search", {"from": agent, "limit": 1})
            if status == 404:
                last, on_bus = None, False
            elif status == 200 and isinstance(data, list):
                last, on_bus = (data[0].get("created_at") if data else None), True
            else:
                raise ApiError(502, _error_text(data, f"The bus answered {status}."))
            cards.append({"agent": agent, "label": row["label"], "host": row["host"],
                          "last_heard": last, "on_bus": on_bus,
                          "unread": (unread or {}).get(agent, 0) if unread is not None else None,
                          "door": app.door(agent, row, policy, policy_err)})
        out = {"agents": cards, "registered": unread is not None}
        if partial:
            out["unread_partial"] = UNREAD_COUNT_PAGE
        if doors_err:
            out["doors_error"] = doors_err
        return out

    def _inbox(self, qs: dict) -> dict:
        app = self.app
        flt = qs.get("filter", ["all"])[0]
        if flt not in ("unread", "all"):
            raise ApiError(400, "filter must be unread or all")
        status, data, headers = app.dispatch(
            "GET", f"/mesh/inbox/{app.cfg.agent}", {"filter": flt, "limit": _limit(qs)})
        if status == 404:
            raise ApiError(409, f"You're not on the bus yet: there's no '{app.cfg.agent}' in the registry.",
                           not_registered=True)
        if status != 200 or not isinstance(data, list):
            raise ApiError(502, _error_text(data, f"The bus answered {status}."))
        letters = []
        for env in data:
            item = slim(env)
            item["outside_door"] = app.outside_door(env)
            letters.append(item)
        return {"letters": letters, "total": _int_header(headers, "X-Inbox-Total", len(letters))}

    def _thread(self, raw_id: str) -> dict:
        msg_id = _positive_int(raw_id, "letter id")
        status, data, _ = self.app.dispatch("GET", f"/mesh/thread/{msg_id}")
        if status == 404:
            raise ApiError(404, "That letter isn't on the bus.")
        if status != 200 or not isinstance(data, dict):
            raise ApiError(502, _error_text(data, f"The bus answered {status}."))
        messages = []
        for env in data.get("messages", []):
            item = slim(env, full=True)
            item["mine"] = env.get("from") == self.app.cfg.agent
            item["outside_door"] = self.app.outside_door(env)
            messages.append(item)
        return {"root_id": data.get("root_id"), "focus": msg_id, "messages": messages}

    def _agent(self, agent: str) -> dict:
        app, me = self.app, self.app.cfg.agent
        doors, _ = app.doors()
        if agent not in doors:
            raise ApiError(404, f"{agent} isn't in the doors file.")
        letters: dict[int, dict] = {}
        for frm, to in ((agent, me), (me, agent)):
            status, data, _ = app.dispatch("GET", "/mesh/search", {"from": frm, "to": to, "limit": 30})
            if status == 404:
                continue          # one side not registered yet: show what exists
            if status != 200 or not isinstance(data, list):
                raise ApiError(502, _error_text(data, f"The bus answered {status}."))
            for env in data:
                item = slim(env)
                item["outside_door"] = app.outside_door(env)
                letters[env["id"]] = item
        newest = sorted(letters.values(), key=lambda m: m["id"], reverse=True)[:30]
        return {"agent": agent, "label": doors[agent]["label"], "letters": newest}

    def _traffic(self, qs: dict) -> dict:
        status, data, _ = self.app.dispatch("GET", "/mesh/history", {"limit": _limit(qs)})
        if status != 200 or not isinstance(data, list):
            raise ApiError(502, _error_text(data, f"The bus answered {status}."))
        return {"letters": [slim(e) for e in data]}

    def _search(self, qs: dict) -> dict:
        q = qs.get("q", [""])[0].strip()
        if not q:
            raise ApiError(400, "Type something to search for.")
        if len(q) > SUBJECT_MAX:
            raise ApiError(400, "That search is too long.")
        # The dispatcher's key is `q` (v0.19.1 rejects `query` at the MCP door).
        status, data, headers = self.app.dispatch("GET", "/mesh/search", {"q": q, "limit": _limit(qs)})
        if status != 200 or not isinstance(data, list):
            raise ApiError(502, _error_text(data, f"The bus answered {status}."))
        return {"letters": [slim(e) for e in data],
                "total": _int_header(headers, "X-Search-Total", len(data))}

    def _holds(self) -> dict:
        app = self.app
        doors, doors_err = app.doors()
        status, data, _ = app.dispatch("GET", "/mesh/pause")
        if status != 200 or not isinstance(data, dict):
            raise ApiError(502, _error_text(data, f"The bus answered {status}."))
        paused = {p.get("agent"): p for p in data.get("paused", []) if isinstance(p, dict)}
        orphans = {o.get("agent"): o.get("held", 0) for o in data.get("held_orphans", []) if isinstance(o, dict)}
        agents = [{"agent": a, "label": row["label"], "paused": a in paused,
                   "paused_at": paused.get(a, {}).get("paused_at"),
                   "held": paused.get(a, {}).get("held", orphans.get(a, 0))}
                  for a, row in doors.items()]
        out = {"agents": agents, "held_orphans": data.get("held_orphans", [])}
        if doors_err:
            out["doors_error"] = doors_err
        return out

    # -- POST (writes; every one is audited) --------------------------------------
    def _send_letters(self, body: dict, device: str) -> dict:
        app, me = self.app, self.app.cfg.agent
        doors, _ = app.doors()
        to = body.get("to")
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list) or not to or not all(isinstance(t, str) for t in to):
            raise ApiError(400, "Pick who the letter is for.")
        to = list(dict.fromkeys(to))                 # one letter per person, even if tapped twice
        unknown = [t for t in to if t not in doors]
        if unknown:
            raise ApiError(400, f"Not in the fleet list: {', '.join(unknown)}")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "The letter is empty.")
        if len(text.encode("utf-8")) > SEND_TEXT_MAX_BYTES:
            raise ApiError(413, "That letter is too long for the phone (16 KB max).")
        subject = body.get("subject")
        if subject is not None and not isinstance(subject, str):
            raise ApiError(400, "The subject must be text.")
        subject = " ".join((subject or "").split())[:SUBJECT_MAX] or derive_subject(text, me)
        reply_to = body.get("reply_to")
        if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to < 1):
            raise ApiError(400, "reply_to must be a letter id.")
        if not app.limiter.reserve(device, len(to)):
            raise ApiError(429, f"That's {SEND_LIMIT} letters in 10 minutes. Wait a few minutes, then try again.")

        receipts = []
        for target in to:
            # S3: `from` is this service's identity, whatever the client sent.
            envelope = {"mesh_version": MESH_VERSION, "from": me, "to": target,
                        "subject": subject, "body": {"text": text, "via": "pocket"}}
            if reply_to is not None:
                envelope["reply_to"] = reply_to
            try:
                status, data, _ = app.dispatch("POST", "/mesh/ping", body=envelope)
            except DispatcherDown:
                status, data = 0, {"error": "Can't reach the bus dispatcher."}
            ok = status in (200, 202) and isinstance(data, dict) and isinstance(data.get("id"), int)
            receipt = {"to": target, "ok": ok, "subject": subject}
            if ok:
                receipt.update(id=data["id"], state=data.get("state"))
            else:
                receipt["error"] = _error_text(data, f"The bus answered {status}.")
            receipts.append(receipt)
            app.audit(route="send", to=target, id=receipt.get("id"), token=device,
                      result=receipt.get("state") if ok else f"error: {receipt['error']}")
        return {"ok": all(r["ok"] for r in receipts), "receipts": receipts}

    def _read(self, raw_id: str, device: str) -> dict:
        app = self.app
        msg_id = _positive_int(raw_id, "letter id")
        status, data, _ = app.dispatch("POST", f"/mesh/read/{msg_id}", body={"agent": app.cfg.agent})
        if status == 200 and isinstance(data, dict):
            app.audit(route="read", id=msg_id, token=device, result="read")
            return {"id": msg_id, "read_at": data.get("read_at"), "first_read": data.get("first_read")}
        app.audit(route="read", id=msg_id, token=device, result=f"error: {status}")
        if status == 403:
            raise ApiError(403, "That letter isn't addressed to you, so it can't be marked read.")
        if status == 404:
            raise ApiError(404, "That letter isn't on the bus.")
        raise ApiError(502, _error_text(data, f"The bus answered {status}."))

    def _brake(self, action: str, body: dict, device: str) -> dict:
        app = self.app
        agent = body.get("agent")
        doors, _ = app.doors()
        if not isinstance(agent, str) or agent not in doors:
            raise ApiError(400, "Pick an agent from the fleet list.")
        status, data, _ = app.dispatch("POST", f"/mesh/{action}", body={"agent": agent})
        if status != 200 or not isinstance(data, dict):
            app.audit(route=action, agent=agent, token=device, result=f"error: {status}")
            raise ApiError(502, _error_text(data, f"The bus answered {status}."))
        if action == "pause":
            app.audit(route="pause", agent=agent, token=device, result=f"paused, {data.get('held', 0)} held")
            return {"agent": agent, "paused": True, "paused_at": data.get("paused_at"),
                    "held": data.get("held", 0)}
        released = data.get("released") or []
        app.audit(route="play", agent=agent, token=device, result=f"playing, released {len(released)}")
        return {"agent": agent, "paused": False, "released": len(released)}


def _positive_int(raw: str, what: str) -> int:
    # isdigit() alone takes "²"; int() then raises. ASCII digits, bounded length.
    if not (raw.isascii() and raw.isdigit()) or len(raw) > ID_MAX_DIGITS or int(raw) < 1:
        raise ApiError(400, f"Bad {what}.")
    return int(raw)


def _int_header(headers, name: str, default: int) -> int:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------- main --

def make_server(cfg: Config, port: int | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((BIND_HOST, cfg.port if port is None else port), Handler)
    server.daemon_threads = True
    server.app = App(cfg)  # type: ignore[attr-defined]
    return server


def mint(name: str, cfg_dir: Path) -> str:
    """Create a device token, store only its hash, return the token (shown once)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise ConfigError("device name: letters, digits, dot, dash, underscore; up to 64 chars")
    cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cfg_dir / "tokens.json"
    table = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if name in table:
        raise ConfigError(f"a token named {name!r} already exists; remove its line first")
    token = "pb-" + secrets.token_urlsafe(32)
    table[name] = hash_token(token)
    fd, tmp = tempfile.mkstemp(dir=cfg_dir, prefix=".tokens-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:     # mkstemp: already 0600
        json.dump(table, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)
    return token


def main(argv: list[str]) -> int:
    if argv[:1] == ["mint"]:
        if len(argv) != 2:
            print("usage: pocket_bus.py mint NAME", file=sys.stderr)
            return 2
        cfg_dir = Path(os.environ.get("POCKET_CONFIG_DIR") or Path.home() / ".config" / "pocket-bus").expanduser()
        try:
            token = mint(argv[1], cfg_dir)
        except ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(token)
        print(f"(stored as a hash in {cfg_dir / 'tokens.json'}; this is the only time it is shown)",
              file=sys.stderr)
        return 0
    if argv:
        print(__doc__.split("Usage:")[1].rstrip(), file=sys.stderr)
        return 2
    try:
        cfg = config_from_env()
    except ConfigError as e:
        log.error("not starting: %s", e)
        return EXIT_CONFIG
    server = make_server(cfg)
    log.info("listening on %s:%d as %s; dispatcher %s", BIND_HOST, cfg.port, cfg.agent, cfg.dispatcher)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
