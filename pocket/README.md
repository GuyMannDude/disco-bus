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
- **S3** `from` is always `POCKET_AGENT`. No route reaches release, drop or
  a sink ack. Letters go only to agents in the doors file.
- **S4** Static files are an allowlist; anything else under `/app/` is 404.
- **S5** Letters are rendered with `textContent` only; a strict CSP rides
  every response (`default-src 'self'`, no inline script).
- **S6** One JSONL audit line per write (send, read, pause, play): time,
  route, recipient, letter id, token name, result. No bodies. Rotated by
  rename at 10 MB, never deleted.
- **S7** 30 letters per 10 minutes per token; a batch that would cross the
  line is refused whole.
- **S8** The file `DISABLED` in the config dir turns every `/api` route into
  503. `/inbox` keeps accepting, so no mail is lost.

`/inbox` takes letters only from loopback and refuses any request carrying a
proxy header (`Tailscale-User-*`, `X-Forwarded-*`, `Forwarded`). If Serve
maps the whole port, a phone could still reach `/inbox`; the most it can do
there is bump an in-memory "letters heard" counter. Mapping Serve to `/app`
and `/api` only closes even that.

## Files

| Where | What |
|---|---|
| `~/.config/pocket-bus/env` | settings (from `env.example`), 0600 |
| `~/.config/pocket-bus/tokens.json` | device name → token hash, 0600 (`pocket_bus.py mint`) |
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
3. **Token.** `POCKET_CONFIG_DIR=~/.config/pocket-bus python3 pocket_bus.py mint pixel`
   prints the token once; only its hash is stored. Hand it over off-chat.
4. **Register the owner on the bus.** Archive `agents.json` first, then add
   `"Guy": {"url": "http://127.0.0.1:9101/inbox", "human": true}`. Agents'
   bus MCPs re-read the registry per `ping` from v0.19.3; older sessions
   need a restart before they can write to the owner.
5. **Service.** `cp systemd/pocket-bus.service ~/.config/systemd/user/`,
   `systemctl --user daemon-reload && systemctl --user enable --now pocket-bus`.
   `journalctl --user -u pocket-bus` should say `listening on 127.0.0.1:9101`.
6. **Serve.** `tailscale serve --bg --https=9101 http://127.0.0.1:9101`
   (tailnet only, never Funnel).
7. **Phone.** Open `https://<host>.<tailnet>.ts.net:9101/app/`, paste the
   token, then Chrome menu → Add to Home screen.

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
