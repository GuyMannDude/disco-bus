# Changelog

## v0.8 — Dispatcher-enforced pause: a paused agent cannot leak a ping by accident

- **Problem:** during planning sessions, agents fire pings mid-discussion —
  half-formed intent escapes and gets superseded minutes later (crossed
  in-flight pings observed same-day). A promise-based hold ("I'll queue my
  drafts") fails exactly when it matters: the enforcement lived in the agent
  that wanted to send.
- **Fix:** pause now lives in the dispatcher. `POST /mesh/pause {agent}` makes
  every subsequent ping FROM that agent land as `state:HELD` — persisted,
  never handed to a delivery thread. `POST /mesh/play {agent}` lifts the pause
  and flushes that agent's held messages one at a time in id order.
  `POST /mesh/release/<id>` ships a single held message while still paused
  (the in-the-open path for genuinely hot items); `POST /mesh/drop/<id>` parks
  it as `DROPPED` — kept in history, never delivered. Both are sender-only.
- HELD/DROPPED are dispatcher-internal: excluded from every inbox view (an
  undelivered draft is not mail, and a HELD reply does not clear unreplied),
  never POSTed to a listener — a flushed message is delivered as a normal
  SENT envelope, so `mesh_version` stays 0.5 and listeners need no change.
- `GET /mesh/pause` reports pause state and held counts for all agents,
  including *orphaned* held rows whose sender is no longer paused (a crash
  window) — a held draft nobody is watching must be visible, not laundered.
  `GET /mesh/held[/<agent>]` lists held envelopes oldest-first.

## v0.7.1 — `ping_read` reports whether THIS call opened the message

- **Problem:** `ping_read` both marks a message read and returns `read_at`, so a
  first read and a hundredth read came back byte-for-byte identical. A reader
  handed a brand-new message id saw a populated `read_at` — the timestamp its own
  call had just written — and reported the message as already read. Observed live:
  Opie stopped acting on fresh bus mail the day v0.7 landed.
- **Fix:** `/mesh/read/{id}` now returns `first_read`, taken from the guarded
  UPDATE's rowcount so a losing racer is correctly told it was not first. The MCP
  `ping_read` leads with a plain-language banner (`NEW —` / `ALREADY READ —`)
  stating that `read_at` on a first read was set by that call.
- `first_read` describes the call, not the row: inbox/history/state listings are
  unchanged and still non-mutating.
- Bumped `robot.info` to match — v0.7 shipped without it, tripping the drift guard.

## v0.7 — Read state separate from reply state

- Added nullable `read_at` with a safe startup migration; existing rows remain unread.
- `ping_read(id)` marks a recipient's message read exactly once.
- `inbox(filter=...)` supports `unread`, `unreplied`, and `all`; default remains `all`.
- Preserved `unread_only=true` as an alias for `filter="unreplied"`.
- Inbox/history/state listing remains non-mutating.

## [Unreleased]

## v0.6.0 — 2026-07-05 — Version anchor (backfill)

Versions 0.2–0.6 shipped before the changelog rule reached this repo; robot.info
carried the version forward without entries here. This entry anchors the current
shipped state so the new robot.info drift-guard test (tests/test_robot_info.py)
has a truth source: mesh v0.5 frozen envelope contract (schema/envelope-v0.5.json),
SQLite-backed push dispatcher + per-agent HTTP listeners, MCP server (ping, inbox,
thread, ping_read, ping_history), optional Discord mirror, install/uninstall
scripts. From here on: every version bump gets an entry BEFORE robot.info moves.

- **Listener bind host is now configurable** (`DISCOBUS_LISTEN_HOST`, default `127.0.0.1`). Problem: the listener hardcoded `HOST = "127.0.0.1"`, so a cross-machine agent could only receive pushes if its listener bound `0.0.0.0` (LAN-exposed) — the same anti-pattern the dispatcher already avoids. Fix: read the bind interface from `DISCOBUS_LISTEN_HOST` so a remote listener can bind a specific Tailscale IP and stay off the home LAN. Default is unchanged, so single-machine setups are unaffected.

## v0.1.0 — Initial public release

**Onboarding** — `install.sh` (interactive wizard for bus-only setup, auto-allocates ports, writes systemd units, smoke-tests end-to-end), `setup-discord.sh` (walks through Discord bot creation + channel ID collection), `uninstall.sh` (clean removal with explicit confirmation before any data deletion). The README's "manual setup" path is now an expandable section — the script path is the default.

**Non-interactive install** — `robot-install.sh` reads a JSON manifest (`robot.install`) and runs the full install with zero prompts. Designed for LLM agents and CI: stdout is a single JSON result object (`{ok, steps: {deps, config, npm, systemd, smoke_test}, error?}`), stderr carries human-readable progress, exit code is 0 on success / 1 on failure. Manifest supports `//` line comments. Agent names are regex-validated. Discord tokens stay out of the manifest (reference an external file via `discord.token_file`). All paths are env-overridable (`DISCOBUS_INSTALL_CONFIG_DIR`, etc.) and a `DISCOBUS_INSTALL_DRY_RUN=1` mode skips systemd write + smoke test for sandboxed testing.

**API additions** (incorporating early review feedback):

- `GET /mesh/inbox/<agent>` — list messages addressed to an agent, newest first. Optional `?unread_only=true` filters to messages with no reply from the recipient.
- `GET /mesh/thread/<id>` — walk the full reply chain. Given any message id, returns the root + every descendant in chronological order.
- `DISCOBUS_MAX_BODY_BYTES` env (default 1 MiB) — reject oversized payloads at the dispatcher boundary.

**MCP server** — two new tools matching the new endpoints:

- `inbox(agent?, limit?, unread_only?)` — defaults to the caller's agent.
- `thread(id)` — full conversation reconstruction.

Total surface: 5 MCP tools (`ping`, `ping_history`, `ping_read`, `inbox`, `thread`).

**Foundation:**

First public cut. Brings together:

- **Dispatcher** (HTTP, SQLite-backed) — `POST /mesh/ping` accepts envelopes, pushes to per-agent listeners, persists state.
- **Per-agent listeners** — tiny HTTP servers, one per agent. Write inbox file on receive. Optional generic auto-reply via subprocess executable (`DISCOBUS_AUTO_REPLY`).
- **Discord mirror** — fire-and-forget side car. Posts full envelope bodies (paginated for Discord's 2000-char per-message limit) to a global firehose channel + per-agent log channels.
- **MCP server** — exposes three tools (`ping`, `ping_history`, `ping_read`) so LLM agents can send, list, and read bus traffic. Identity bound by env; can't spoof `from`.
- **Schema** — frozen v0.5 envelope, JSON Schema. Mismatched versions are rejected.
- **Systemd units** — user-mode services for dispatcher + per-agent listeners.

Distilled from a private internal build that has been running 24/7. Sanitized for general use: agent names are no longer hardcoded, all paths are env-overridable, secrets ship via separate files.
