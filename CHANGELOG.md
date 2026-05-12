# Changelog

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
