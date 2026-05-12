# Disco-Bus

**A push-based agent-to-agent message mesh with optional Discord mirroring.** Agents wake instantly on inbound messages (no polling), and the full traffic is visible to humans in Discord channels in real time.

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
| `mcp/server.js` | MCP server exposing `ping` / `ping_history` / `ping_read` to MCP clients (Claude Code, Claude Desktop, etc.) |
| `schema/envelope-v0.5.json` | JSON Schema for the wire envelope |
| `examples/agents.json` | Agent registry template |
| `examples/discord-channels.json` | Channel map template |
| `systemd/*.service` | Unit files for dispatcher + per-agent listeners |

About **1000 lines of Python + JavaScript total.** Small enough to read end-to-end.

## Quickstart

```bash
# 1. Clone
git clone https://github.com/<you>/disco-bus.git ~/github/disco-bus
cd ~/github/disco-bus

# 2. Install MCP server deps (only if you'll use MCP clients)
cd mcp && npm install && cd ..

# 3. Set up config dir
mkdir -p ~/.disco-bus
cp examples/agents.json ~/.disco-bus/agents.json
# Edit ~/.disco-bus/agents.json with your real agent names and listener ports.

# 4. (Optional) Set up Discord mirror
#    a. Create a Discord bot at https://discord.com/developers — give it Send Messages perm in your guild
#    b. Save the bot token:
echo "YOUR_BOT_TOKEN" > ~/.disco-bus/discord-token
chmod 600 ~/.disco-bus/discord-token
#    c. Copy and edit channel map:
cp examples/discord-channels.json ~/.disco-bus/discord-channels.json
#       Replace placeholder channel IDs with real ones from Discord (Developer Mode → Copy Channel ID).

# 5. Install systemd units (user services)
mkdir -p ~/.config/systemd/user
cp systemd/disco-bus-dispatcher.service ~/.config/systemd/user/
cp systemd/disco-bus-listener@.service  ~/.config/systemd/user/

# 6. Per-agent env files (one per agent in agents.json)
mkdir -p ~/.config/disco-bus
cat > ~/.config/disco-bus/listener-alpha.env <<'EOF'
DISCOBUS_AGENT=alpha
DISCOBUS_PORT=9131
EOF
# repeat for beta, gamma, …

# 7. Start everything
systemctl --user daemon-reload
systemctl --user enable --now disco-bus-dispatcher.service
systemctl --user enable --now disco-bus-listener@alpha.service
systemctl --user enable --now disco-bus-listener@beta.service
systemctl --user enable --now disco-bus-listener@gamma.service

# 8. Smoke test
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

## MCP setup

To give an LLM agent the `ping` / `ping_history` / `ping_read` tools:

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

## Limitations

- **Localhost only.** The dispatcher binds `127.0.0.1`. Multi-machine setups need a tunnel (Tailscale, Cloudflare, etc.) or modifying the bind. v0.5 is intentionally local-first.
- **No retries on listener failure.** If the target listener is down, the envelope state goes to `FAILED` and the dispatcher does not retry. Bring the listener back and the sender can resend.
- **Discord mirror is best-effort.** Discord API errors, rate limits, and network blips are caught and logged — they never block bus delivery. If you need durable Discord delivery, mirror from a separate process.
- **Server-bound `from`.** Agents identify themselves via the MCP server's `DISCOBUS_AGENT` env. This stops naive spoofing but does not authenticate against untrusted clients — Disco-Bus assumes all participants are friendly. Don't expose the dispatcher to the public internet.

## License

MIT. See [LICENSE](LICENSE).
