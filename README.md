# Disco-Bus

**A push-based agent-to-agent message mesh with optional Discord mirroring.** Agents wake instantly on inbound messages (no polling), and the full traffic is visible to humans in Discord channels in real time.

> [!TIP]
> **AI agents — start here.**
>
> - **`robot.info`** — structured JSON manifest describing this product:
>   identity, capabilities, REST endpoints + 5 MCP tools, install
>   pointer, compatibility matrix, and 10 common-question/answer
>   pairs (Why not Redis? Does it need Discord? What if an agent is
>   offline? — and more). Read this first to answer a user's questions
>   about Disco-Bus without scraping the README. Spec:
>   [mnemo-cortex/ROBOT-INFO-SPEC.md](https://github.com/GuyMannDude/mnemo-cortex/blob/master/ROBOT-INFO-SPEC.md).
> - **`robot.install`** + **`./robot-install.sh`** — non-interactive
>   setup. Edit the manifest (defaults are sensible), run the installer,
>   parse the JSON object on stdout. Zero prompts, structured progress
>   on stderr, exit `0` on success. Full schema in "Non-interactive
>   install (for LLM agents and CI)" below.

```
       agent A                                agent B
          │                                      ▲
          │ ping_send()                          │  POST /inbox
          ▼                                      │
   ┌──────────────┐         POST            ┌─────────┐
   │  Dispatcher  ├────────────────────────►│ Listener│
   │   :9100      │                         │   :9132 │
   └─────┬────────┘                         └─────────┘
         │  (also)
         ▼
   ┌────────────────┐
   │  Discord       │  full envelope bodies, paginated
   │  #agent-a-log  │  global firehose + per-agent channels
   │  #agent-b-log  │
   │  #dispatch     │
   └────────────────┘
```

## Why this exists

Most agent meshes I'd seen used one of three weak delivery models:
- **Polling** — agent walks an inbox every N seconds; slow + wastes cycles
- **Auto-spawn** — a fresh agent process is started per message; works, but has no continuity
- **Pure pub/sub** — agents must already be subscribed; if they're not running, the message is lost

Disco-Bus picks a fourth: **direct push to a tiny per-agent HTTP listener that always runs**. The listener writes the envelope to a per-agent inbox directory AND (optionally) fires an auto-reply executable. Stateful agents come back and read their inbox when they're ready; stateless agents react instantly via the auto-reply hook.

Discord exists for *humans* — so you can watch agent-to-agent traffic in real time without opening JSON files.

## Components

| Component | What it does |
|---|---|
| `dispatcher/dispatcher.py` | Accepts `POST /mesh/ping`, persists to SQLite, pushes to per-agent listeners, posts to Discord |
| `dispatcher/discord_mirror.py` | Discord side car — fire-and-forget, never blocks delivery |
| `listeners/listener.py` | Per-agent HTTP listener. Writes inbox file. Optional auto-reply via subprocess. |
| `mcp/server.js` | MCP server exposing `ping` / `ping_history` / `ping_read` / `inbox` / `thread` to MCP clients (Claude Code, Claude Desktop, etc.) |
| `install.sh` / `setup-discord.sh` / `uninstall.sh` | Interactive setup scripts |
| `schema/envelope-v0.5.json` | JSON Schema for the wire envelope |
| `examples/agents.json` | Agent registry template |
| `examples/discord-channels.json` | Channel map template |
| `systemd/*.service` | Unit files for dispatcher + per-agent listeners |

About **1000 lines of Python + JavaScript total.** Small enough to read end-to-end.

## Quickstart

```bash
git clone https://github.com/<you>/disco-bus.git ~/github/disco-bus
cd ~/github/disco-bus
./install.sh
```

That's it. The wizard asks what agents you want, allocates ports, writes configs, installs systemd user services, starts everything, and smoke-tests with a real ping.

**Want Discord mirroring too?** (You can watch agent-to-agent traffic in real time in Discord channels.) Run this after `install.sh`:

```bash
./setup-discord.sh
```

It walks you through creating a Discord bot (60-second one-time setup) and asks for channel IDs.

**Want to remove everything?**

```bash
./uninstall.sh
```

Stops services, removes systemd units, and asks before deleting any data. The source repo stays put.

### Non-interactive install (for LLM agents and CI)

Don't want prompts? Fill out a JSON manifest and run `robot-install.sh`.

```bash
# Edit robot.install (or make a copy) — see schema below
./robot-install.sh robot.install
```

The script emits a single JSON object on stdout for the caller to parse. All human-readable progress goes to stderr.

```json
{
  "ok": true,
  "steps": {
    "deps":       {"ok": true, "python": "3.12"},
    "config":     {"ok": true, "agents": {"alice": "http://127.0.0.1:9131/inbox", ...}},
    "npm":        {"ok": true},
    "systemd":    {"ok": true, "dispatcher_port": 9100},
    "smoke_test": {"ok": true, "msg_id": 1, "final_state": "DELIVERED"}
  }
}
```

On failure, `ok` is `false`, exit code is `1`, and `error` describes which step blew up.

**Manifest schema (all fields optional except `agents`):**

```json
{
  "dispatcher": {
    "port": 9100,
    "max_body_bytes": 1048576
  },
  "agents": [
    {"name": "alice"},
    {"name": "bob", "port": 9132}
  ],
  "listener_port_start": 9131,
  "discord": {
    "enabled": false,
    "token_file": "/path/to/your/discord-token",
    "global_channel_id": 0,
    "agent_channels": {"alice": 1234567890123456789}
  },
  "mcp": { "install_deps": true },
  "smoke_test": { "enabled": true, "from": null, "to": null }
}
```

Notes:
- Agent names must match `^[a-zA-Z][a-zA-Z0-9_-]{0,30}$`. The script validates and rejects bad names with a clear error.
- If an agent has no `port`, it gets auto-allocated starting from `listener_port_start`.
- **Discord tokens are NOT embedded in the manifest.** Set `token_file` to a path the script can read; the token is copied to `~/.disco-bus/discord-token` with `chmod 600`. This keeps secrets out of any version control.
- `// line comments` are stripped before JSON parsing, so you can annotate your manifest.
- Re-runnable: existing agents are preserved; new ones are merged in. Existing env files are kept as-is.

**Sandbox testing:** override paths via env so you can dry-run without touching real state:

```bash
DISCOBUS_INSTALL_CONFIG_DIR=/tmp/test-config \
DISCOBUS_INSTALL_ENV_DIR=/tmp/test-env \
DISCOBUS_INSTALL_SYSTEMD_DIR=/tmp/test-systemd \
DISCOBUS_INSTALL_DRY_RUN=1 \
./robot-install.sh /path/to/manifest.json
```

`DRY_RUN=1` runs through dependency check + config write + npm install but skips writing systemd units and the smoke test.

### Manual setup (skip the scripts)

<details><summary>If you'd rather do it by hand, click to expand.</summary>

```bash
# 1. MCP deps
cd mcp && npm install && cd ..

# 2. Config
mkdir -p ~/.disco-bus
cp examples/agents.json ~/.disco-bus/agents.json
# Edit ~/.disco-bus/agents.json with your real agent names and listener ports.

# 3. (Optional) Discord
echo "YOUR_BOT_TOKEN" > ~/.disco-bus/discord-token
chmod 600 ~/.disco-bus/discord-token
cp examples/discord-channels.json ~/.disco-bus/discord-channels.json
# Replace placeholder channel IDs with real ones from Discord (Developer Mode → Copy Channel ID).

# 4. systemd units
mkdir -p ~/.config/systemd/user ~/.config/disco-bus
cp systemd/disco-bus-dispatcher.service ~/.config/systemd/user/
cp systemd/disco-bus-listener@.service  ~/.config/systemd/user/

# Per-agent env files
cat > ~/.config/disco-bus/listener-alpha.env <<'EOF'
DISCOBUS_AGENT=alpha
DISCOBUS_PORT=9131
EOF
# repeat for each agent

# 5. Start
systemctl --user daemon-reload
systemctl --user enable --now disco-bus-dispatcher.service
systemctl --user enable --now disco-bus-listener@alpha.service
# ...

# 6. Smoke test
curl -X POST http://127.0.0.1:9100/mesh/ping -H "Content-Type: application/json" -d '{
  "mesh_version": "0.5",
  "from": "alpha",
  "to": "beta",
  "subject": "hello-world",
  "body": {"greeting": "first message on the bus"}
}'
# Should return: {"id": 1, "tracking_id": "msg-1-...", "state": "SENT"}
# A file appears at ~/.disco-bus/inbox/beta/msg-1-*.json
# The envelope shows up in Discord (if mirror is configured).
```

</details>

## MCP setup

To give an LLM agent the bus tools (`ping`, `ping_history`, `ping_read`, `inbox`, `thread`):

**Claude Desktop (`~/.config/Claude/claude_desktop_config.json`):**

```json
{
  "mcpServers": {
    "disco-bus": {
      "command": "node",
      "args": ["/home/<you>/github/disco-bus/mcp/server.js"],
      "env": {
        "DISCOBUS_AGENT": "alpha",
        "DISCOBUS_DISPATCHER": "http://127.0.0.1:9100"
      }
    }
  }
}
```

**Claude Code (`~/.claude/settings.json` mcpServers section):** same shape.

Each MCP instance is identity-bound: it can only send `from: <DISCOBUS_AGENT>`. Callers cannot spoof the sender.

### Tools

| Tool | What it does |
|---|---|
| `ping(to, subject, body, reply_to?)` | Send a message. Wakes the recipient's listener immediately. |
| `ping_history(limit?)` | List recent envelopes across all agents, newest first. Summary form. |
| `ping_read(id)` | Fetch the full envelope (including body) for one message id. |
| `inbox(agent?, limit?, unread_only?)` | List messages addressed to an agent (default: this one). `unread_only=true` filters to messages you haven't replied to. |
| `thread(id)` | Walk the entire reply chain — give any message id, get the whole conversation in chronological order. |

## Auto-reply (optional)

A listener can fire a shell command per inbound message to compose an auto-reply. Set `DISCOBUS_AUTO_REPLY` in the listener's env file to an executable path. The executable receives the envelope JSON on stdin; its stdout becomes the reply body.

```bash
# ~/.config/disco-bus/listener-alpha.env
DISCOBUS_AGENT=alpha
DISCOBUS_PORT=9131
DISCOBUS_AUTO_REPLY=/usr/local/bin/alpha-auto-reply.sh
DISCOBUS_AUTO_REPLY_TIMEOUT=120
```

```bash
#!/usr/bin/env bash
# alpha-auto-reply.sh — receives envelope JSON on stdin, emits reply body on stdout
envelope=$(cat)
# Do whatever — call an LLM, run a tool, look something up...
echo '{"ack":true,"note":"received and processed"}'
```

The auto-reply only fires for new pings (not replies), to avoid infinite loops.

> **Caveat:** auto-reply uses a fresh subprocess per message. There's no shared memory between invocations. If your agent needs continuity, write to the inbox and let the stateful agent read it on its own schedule instead.

## Wire protocol

See `schema/envelope-v0.5.json`. v0.5 has three states (`SENT → DELIVERED | FAILED`) and a frozen envelope shape. Listeners reject any envelope where `mesh_version != "0.5"`.

## Configuration reference

All paths can be overridden via env. Defaults in `~/.disco-bus/`.

| Var | Default | Purpose |
|---|---|---|
| `DISCOBUS_HOST` | `127.0.0.1` | Dispatcher bind address |
| `DISCOBUS_PORT` | `9100` | Dispatcher port (and MCP `DISCOBUS_DISPATCHER` target) |
| `DISCOBUS_DB` | `~/.disco-bus/disco-bus.sqlite` | SQLite state |
| `DISCOBUS_AGENTS_FILE` | `~/.disco-bus/agents.json` | Agent registry |
| `DISCOBUS_INBOX` | `~/.disco-bus/inbox` | Per-agent inbox root |
| `DISCOBUS_DISCORD_TOKEN_FILE` | `~/.disco-bus/discord-token` | Discord bot token file |
| `DISCOBUS_CHANNELS` | `~/.disco-bus/discord-channels.json` | Channel map |
| `DISCOBUS_AGENT` | (required, no default) | Agent identity for listener + MCP |
| `DISCOBUS_AUTO_REPLY` | (unset = disabled) | Optional auto-reply command |
| `DISCOBUS_AUTO_REPLY_TIMEOUT` | `120` | Auto-reply timeout in seconds |
| `DISCOBUS_MAX_BODY_BYTES` | `1048576` (1 MiB) | Reject envelopes whose JSON-encoded body exceeds this size |

## Limitations

- **Localhost only.** The dispatcher binds `127.0.0.1`. Multi-machine setups need a tunnel (Tailscale, Cloudflare, etc.) or modifying the bind. v0.5 is intentionally local-first.
- **No retries on listener failure.** If the target listener is down, the envelope state goes to `FAILED` and the dispatcher does not retry. Bring the listener back and the sender can resend.
- **Discord mirror is best-effort.** Discord API errors, rate limits, and network blips are caught and logged — they never block bus delivery. If you need durable Discord delivery, mirror from a separate process.
- **Server-bound `from`.** Agents identify themselves via the MCP server's `DISCOBUS_AGENT` env. This stops naive spoofing but does not authenticate against untrusted clients — Disco-Bus assumes all participants are friendly. Don't expose the dispatcher to the public internet.

## License

MIT. See [LICENSE](LICENSE).
