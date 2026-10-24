# Pocket Bus

A phone face for the bus, and one human's mailbox on it.

The bus already stores every letter. Pocket Bus adds the two things a person
on the bus was missing: a web app for their phone, and a **listener**, so
letters to them are DELIVERED like any agent's instead of piling up or being
swept as a sink's.

One small service (`pocket_bus.py`, Python standard library only):

| Path | What it is |
|---|---|
| `/app/*` | the phone app: a closed list of static files, nothing private in them |
| `/api/*` | token-checked JSON for the app; every bus call is made as the owner (`POCKET_AGENT`), never as whoever the client claims to be |
| `/inbox` | the owner's listener: the dispatcher POSTs letters here, over loopback |

The dispatcher stays the store. Pocket Bus writes no mail to disk; its only
files are its config (tokens, doors, kill switch) and an audit log of writes.

## Screens

**Fleet** (one card per agent: last heard, unread count, its real door) ·
**Inbox** · **Conversation** (bubbles, Show more on long letters, raw JSON
only under Details) · **Write** (one letter per chosen agent, a receipt per
letter from the dispatcher's reply) · **Traffic** (the whole bus, plus
search) · **Hold** (Pause / Play an agent's outgoing letters). Polls every
30 s only while the page is open and visible. Offline, the service worker's
shell says the bus can't be reached and when it last was.

## Security

- **S1** Binds `127.0.0.1` only, not configurable. The phone reaches it
  through Tailscale Serve, which adds `Tailscale-User-Login` and strips
  client-supplied copies.
- **S2** **The per-device token is the gate.** `tokens.json` (0600) holds
  only SHA-256 hashes, one named line per device; delete a line and that
  device is out on its next request. A loose-permission or unreadable token
  file fails closed (500).
  **What the login check does:** `Tailscale-User-Login` must equal
  `POCKET_TAILSCALE_LOGIN`. That is defense-in-depth only. When every
  tailnet node signs in as the same person it cannot tell one of their
  devices from another. It does shut out tagged devices, users a node is
  shared with, and Funnel traffic (no header).
- **S3** `from` is always `POCKET_AGENT`. Letters go only to agents in the
  doors file, so no route reaches a sink ack as long as the doors file lists
  no sink (keep sinks out of it); no route reaches release or drop at all.
- **S4** Static files are an allowlist; anything else under `/app/` is 404.
- **S5** Letters are rendered with `textContent` only; a strict CSP rides
  every response (`default-src 'self'`, no inline script).
- **S6** One JSONL audit line per write (send, read, pause, play): time,
  route, recipient, letter id, token name, result. No bodies. Pairing tries
  get one line each too (time, device name, result, wrong tries so far;
  never the code or the token). Rotated by rename at 10 MB, never deleted.
- **S7** 30 letters per 10 minutes per token; a batch that would cross the
  line is refused whole.
- **S8** The file `DISABLED` in the config dir turns every `/api` route into
  503. `/inbox` keeps accepting, so no mail is lost.
- **S9** **Pairing: six digits instead of a pasted token.**
  `pocket_bus.py --pair NAME` prints a one-time 6-digit code and stores only
  its SHA-256 in `pairing.json` (0600), good for 10 minutes. `POST /api/pair
  {code}` is the one `/api` route without a bearer: the login check (403),
  the kill switch (503) and the audit still apply. A match mints a new token
  for NAME (the old `tokens.json` is archived first if NAME already has a
  line), burns the code, and returns the token once. Five wrong tries burn
  the code; expired or burned is 410. Every phone reaches the service as
  127.0.0.1 through Serve, so on top of that one shared window caps pairing
  at 20 tries per 10 minutes. A POST that isn't `application/json` is 415:
  that forces a browser preflight (405), so a web page can't burn codes.
  The service and the CLI share one lock (`.lock`) for every file rewrite.
  A 6-digit hash is no secret to someone who can
  read the file; the 0600 mode and the 10 minutes are what protect it.

`/inbox` takes letters only from loopback, only as `application/json`, and
refuses any request carrying an `Origin` (a web page) or a proxy header
(`Tailscale-User-*`, `X-Forwarded-*`, `Forwarded`). The most a caller that
gets past that can do is bump an in-memory "letters heard" counter. If Serve
maps the whole port, mapping it to `/app` and `/api` only closes even that;
either way, check at deploy that a POST to `/inbox` through Serve gets 403.

## Files

| Where | What |
|---|---|
| `~/.config/pocket-bus/env` | settings (from `env.example`), 0600 |
| `~/.config/pocket-bus/tokens.json` | device name → token hash, 0600 (`pocket_bus.py mint`, or pairing) |
| `~/.config/pocket-bus/pairing.json` | pending pairing codes: name → code hash, expiry, wrong tries, 0600 (`pocket_bus.py --pair`) |
| `~/.config/pocket-bus/doors.json` | the fleet list: one row per agent (from `doors.example.json`) |
| `~/.config/pocket-bus/DISABLED` | kill switch: present = `/api` off |
| `~/.local/state/pocket-bus/audit.jsonl` | write audit |

**Doors.** Each row is `{label, host, wakes, how}`, keyed by bus id. `how`
is plain words for how to reach that agent when a letter alone won't wake
it. A `wakes: true` that isn't true is a defect: flip it in the same change
that wires the agent's wake. If `POCKET_WAKE_POLICY` (default
`<repo>/wake/wake-policy.json`) exists, it decides `wakes` and the card
notes any disagreement with the doors file.

## Deploy (for the bus operator)

1. **Settings.** `mkdir -p ~/.config/pocket-bus && chmod 700 ~/.config/pocket-bus`,
   copy `env.example` to `~/.config/pocket-bus/env`, fill in
   `POCKET_DISCOBUS_URL` (the address the bus bridges use, not
   `127.0.0.1` unless the dispatcher really listens there) and
   `POCKET_TAILSCALE_LOGIN`, `chmod 600` it.
2. **Doors.** Write `~/.config/pocket-bus/doors.json` from your fleet.
3. **Token.** Usually nothing to do here: step 7 pairs the phone. For a
   device that can't type a code, `POCKET_CONFIG_DIR=~/.config/pocket-bus
   python3 pocket_bus.py mint pixel` prints a token once (only its hash is
   stored); hand it over off-chat.
4. **Register the owner on the bus.** Archive `agents.json` first, then add
   `"Guy": {"url": "http://127.0.0.1:9101/inbox", "human": true}`. Agents'
   bus MCPs re-read the registry per `ping` from v0.19.3; older sessions
   need a restart before they can write to the owner.
5. **Service.** `cp systemd/pocket-bus.service ~/.config/systemd/user/`,
   `systemctl --user daemon-reload && systemctl --user enable --now pocket-bus`.
   `journalctl --user -u pocket-bus` should say `listening on 127.0.0.1:9101`.
6. **Serve.** `tailscale serve --bg --https=9101 http://127.0.0.1:9101`
   (tailnet only, never Funnel).
7. **Phone.** Open `https://<host>.<tailnet>.ts.net:9101/app/`. On the bus
   host run `python3 pocket_bus.py --pair pixel` and read the six digits to
   the owner; they type them on the phone (within 10 minutes) and it lands
   on Fleet. Then Chrome menu → Add to Home screen. ("Paste a token instead"
   under the code field still takes a minted token.)

**Lost phone:** delete its line in `tokens.json`. **Everything off:**
`touch ~/.config/pocket-bus/DISABLED`.

## Tests

```
python3 -m pytest -q pocket/tests
```

Against a stub dispatcher with the real one's routes, plus the real
dispatcher module in-process for the full loop. `test_app_dom.py` drives
headless Chrome over CDP (skips without `google-chrome` and `node`); set
`POCKET_SCREENSHOTS=<dir>` to save a phone-size picture of every screen.
