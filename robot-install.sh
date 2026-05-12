#!/usr/bin/env bash
# robot-install.sh — non-interactive Disco-Bus installer driven by a JSON manifest.
#
# Usage:
#   ./robot-install.sh [path/to/manifest.json]
#   default manifest: ./robot.install
#
# Designed for LLM agents and CI: zero prompts, all human-readable progress on
# stderr, a single JSON object on stdout for the caller to parse.
#
# Stdout shape (always valid JSON):
#   {
#     "ok": true|false,
#     "steps": {
#       "deps":       {"ok": true},
#       "config":     {"ok": true, "agents": {"alice": "http://127.0.0.1:9131/inbox", ...}},
#       "npm":        {"ok": true},
#       "systemd":    {"ok": true, "dispatcher_port": 9100},
#       "smoke_test": {"ok": true, "msg_id": 1, "final_state": "DELIVERED"}
#     },
#     "error": "<reason>"        // only present when ok=false
#   }
#
# Exit codes:
#   0 — success (ok:true)
#   1 — failure (ok:false; error field describes which step blew up)
#
# Env overrides (for testing / sandboxed installs):
#   DISCOBUS_INSTALL_CONFIG_DIR   default ~/.disco-bus
#   DISCOBUS_INSTALL_ENV_DIR      default ~/.config/disco-bus
#   DISCOBUS_INSTALL_SYSTEMD_DIR  default ~/.config/systemd/user
#   DISCOBUS_INSTALL_DRY_RUN      "1" to skip systemd write/enable (validates config + npm only)

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${1:-${REPO_DIR}/robot.install}"
CONFIG_DIR="${DISCOBUS_INSTALL_CONFIG_DIR:-${HOME}/.disco-bus}"
ENV_DIR="${DISCOBUS_INSTALL_ENV_DIR:-${HOME}/.config/disco-bus}"
SYSTEMD_DIR="${DISCOBUS_INSTALL_SYSTEMD_DIR:-${HOME}/.config/systemd/user}"
DRY_RUN="${DISCOBUS_INSTALL_DRY_RUN:-0}"

log() { printf '[disco-bus] %s\n' "$*" >&2; }

# Emit a single JSON result and exit. STEPS is the in-progress steps object.
emit() {
  local ok="$1" error="${2:-}"
  python3 - "$ok" "$error" "$STEPS" <<'PY'
import json, sys
ok = sys.argv[1] == "true"
err = sys.argv[2]
steps = json.loads(sys.argv[3])
out = {"ok": ok, "steps": steps}
if not ok and err:
    out["error"] = err
print(json.dumps(out, indent=2))
PY
  [ "$ok" = "true" ] && exit 0 || exit 1
}

STEPS='{}'

# Add or replace a key in $STEPS. Value must be valid JSON.
set_step() {
  local key="$1" value="$2"
  STEPS=$(python3 - "$STEPS" "$key" "$value" <<'PY'
import json, sys
steps = json.loads(sys.argv[1])
steps[sys.argv[2]] = json.loads(sys.argv[3])
print(json.dumps(steps))
PY
)
}

# ─── parse manifest into shell vars via one python3 call ──────────────

if ! command -v python3 >/dev/null 2>&1; then
  printf '{"ok": false, "error": "python3 not found", "steps": {}}\n'
  exit 1
fi

PARSED_ENV=$(python3 - "$MANIFEST" <<'PY'
import json, sys, re
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print(f"__ERROR__=manifest not found: {path}")
    sys.exit(0)

try:
    raw = path.read_text()
    # Allow JS-style // line comments (strip them) so callers can annotate
    # manifests. Block comments and inline values containing "//" are NOT
    # stripped — keep the rule simple.
    cleaned = "\n".join(
        re.sub(r"^\s*//.*$", "", line) for line in raw.splitlines()
    )
    data = json.loads(cleaned)
except json.JSONDecodeError as e:
    print(f"__ERROR__=invalid JSON in {path}: {e}")
    sys.exit(0)

def shquote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"

disp = data.get("dispatcher") or {}
print(f"DISPATCHER_PORT={int(disp.get('port', 9100))}")
print(f"MAX_BODY_BYTES={int(disp.get('max_body_bytes', 1048576))}")

agents = data.get("agents") or []
name_re = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,30}$")
agent_records = []
for a in agents:
    if not isinstance(a, dict) or "name" not in a:
        print(f"__ERROR__=each agent must be an object with a 'name' field, got {a!r}")
        sys.exit(0)
    name = a["name"]
    if not name_re.match(name):
        print(f"__ERROR__=invalid agent name {name!r} (must start with a letter; letters, digits, _ and - only; max 31 chars)")
        sys.exit(0)
    port = a.get("port")
    agent_records.append({"name": name, "port": int(port) if port else None})

print(f"AGENTS_JSON={shquote(json.dumps(agent_records))}")
print(f"LISTENER_PORT_START={int(data.get('listener_port_start', 9131))}")

discord = data.get("discord") or {}
print(f"DISCORD_ENABLED={'1' if discord.get('enabled') else '0'}")
token_file = discord.get("token_file") or ""
print(f"DISCORD_TOKEN_FILE={shquote(token_file)}")
print(f"DISCORD_GLOBAL_ID={int(discord.get('global_channel_id', 0) or 0)}")
print(f"DISCORD_AGENT_CHANNELS_JSON={shquote(json.dumps(discord.get('agent_channels') or {}))}")

mcp = data.get("mcp") or {}
print(f"MCP_INSTALL_DEPS={'1' if mcp.get('install_deps', True) else '0'}")

st = data.get("smoke_test") or {}
print(f"SMOKE_ENABLED={'1' if st.get('enabled', True) else '0'}")
print(f"SMOKE_FROM={shquote(st.get('from') or '')}")
print(f"SMOKE_TO={shquote(st.get('to') or '')}")
PY
)

if echo "$PARSED_ENV" | grep -q '^__ERROR__='; then
  err=$(echo "$PARSED_ENV" | sed -n 's/^__ERROR__=//p')
  set_step manifest "{\"ok\": false}"
  emit false "$err"
fi

eval "$PARSED_ENV"
log "manifest parsed: $MANIFEST"

# ─── step 1/5: dependency check ───────────────────────────────────────

log "step 1/5 — checking dependencies"

missing=""
for c in python3 node npm curl systemctl; do
  command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
done

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')
[ "$PY_OK" = "1" ] || missing="$missing python>=3.10(have_$PY_VER)"

if [ -n "${missing// }" ]; then
  set_step deps "{\"ok\": false, \"missing\":\"${missing# }\"}"
  emit false "missing dependencies:${missing}"
fi

if ! systemctl --user list-units >/dev/null 2>&1; then
  set_step deps '{"ok": false, "missing": "systemctl --user not functional"}'
  emit false "systemctl --user not working (try: loginctl enable-linger \$USER)"
fi

set_step deps "{\"ok\": true, \"python\": \"$PY_VER\"}"

# ─── step 2/5: agents.json + listener env files ───────────────────────

log "step 2/5 — writing config to $CONFIG_DIR"

mkdir -p "$CONFIG_DIR" "$ENV_DIR"

CONFIG_RESULT=$(python3 - "$CONFIG_DIR/agents.json" "$LISTENER_PORT_START" "$AGENTS_JSON" "$ENV_DIR" <<'PY'
import json, sys, socket
from pathlib import Path

agents_path = Path(sys.argv[1])
port_start = int(sys.argv[2])
new_agents = json.loads(sys.argv[3])
env_dir = Path(sys.argv[4])

data = {}
if agents_path.exists():
    try:
        data = json.loads(agents_path.read_text())
        data.pop("_comment", None)
    except Exception:
        data = {}

used_ports = set()
for cfg in data.values():
    url = cfg.get("url", "")
    if ":" in url:
        try:
            used_ports.add(int(url.rsplit(":", 1)[-1].split("/")[0]))
        except ValueError:
            pass

def port_free(p):
    if p in used_ports:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", p))
        s.close()
        return True
    except OSError:
        return False

result = {}
next_port = port_start
for a in new_agents:
    name = a["name"]
    if name in data:
        result[name] = data[name]["url"]
        continue
    if a.get("port"):
        port = int(a["port"])
        if not port_free(port):
            print(json.dumps({"error": f"requested port {port} for '{name}' is in use"}))
            sys.exit(0)
    else:
        while not port_free(next_port):
            next_port += 1
        port = next_port
        next_port += 1
    url = f"http://127.0.0.1:{port}/inbox"
    data[name] = {"url": url}
    used_ports.add(port)
    result[name] = url

agents_path.write_text(json.dumps(data, indent=2))

# Per-agent env files — only write if missing (preserve user customization)
env_dir.mkdir(parents=True, exist_ok=True)
for name in data.keys():
    env_file = env_dir / f"listener-{name}.env"
    if not env_file.exists():
        port = int(data[name]["url"].rsplit(":", 1)[-1].split("/")[0])
        env_file.write_text(f"DISCOBUS_AGENT={name}\nDISCOBUS_PORT={port}\n")

print(json.dumps({"agents": result, "registry_path": str(agents_path)}))
PY
)

if echo "$CONFIG_RESULT" | python3 -c 'import sys,json;sys.exit(0 if "error" not in json.load(sys.stdin) else 1)'; then
  AGENTS_MAP=$(echo "$CONFIG_RESULT" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["agents"]))')
  set_step config "{\"ok\": true, \"agents\": ${AGENTS_MAP}}"
  ALL_AGENTS=$(echo "$AGENTS_MAP" | python3 -c 'import sys,json;print(" ".join(json.load(sys.stdin).keys()))')
else
  err=$(echo "$CONFIG_RESULT" | python3 -c 'import sys,json;print(json.load(sys.stdin)["error"])')
  set_step config "{\"ok\": false, \"error\": \"$err\"}"
  emit false "config: $err"
fi

# Discord config — only if enabled AND token_file points at a readable file
if [ "$DISCORD_ENABLED" = "1" ]; then
  if [ -z "$DISCORD_TOKEN_FILE" ] || [ ! -r "$DISCORD_TOKEN_FILE" ]; then
    set_step discord "{\"ok\": false, \"error\": \"discord.enabled=true but token_file missing or unreadable\"}"
    emit false "discord enabled but token_file is missing or unreadable: '$DISCORD_TOKEN_FILE'"
  fi
  cp "$DISCORD_TOKEN_FILE" "$CONFIG_DIR/discord-token"
  chmod 600 "$CONFIG_DIR/discord-token"
  python3 - "$CONFIG_DIR/discord-channels.json" "$DISCORD_GLOBAL_ID" "$DISCORD_AGENT_CHANNELS_JSON" <<'PY'
import json, sys
out = {}
g = int(sys.argv[2])
if g:
    out["global"] = g
agents = {k: int(v) for k, v in json.loads(sys.argv[3]).items() if int(v)}
if agents:
    out["agents"] = agents
with open(sys.argv[1], "w") as f:
    json.dump(out, f, indent=2)
PY
  set_step discord '{"ok": true}'
  log "discord config written"
fi

# ─── step 3/5: npm install ────────────────────────────────────────────

log "step 3/5 — npm"

if [ "$MCP_INSTALL_DEPS" = "1" ]; then
  if [ -d "$REPO_DIR/mcp/node_modules" ]; then
    set_step npm '{"ok": true, "note": "node_modules already present"}'
    log "  node_modules present, skipping"
  else
    if ( cd "$REPO_DIR/mcp" && npm install --silent >&2 ); then
      set_step npm '{"ok": true}'
    else
      set_step npm '{"ok": false, "error": "npm install failed"}'
      emit false "npm install failed"
    fi
  fi
else
  set_step npm '{"ok": true, "skipped": true}'
fi

# ─── step 4/5: systemd units ──────────────────────────────────────────

if [ "$DRY_RUN" = "1" ]; then
  log "step 4/5 — systemd (skipped, DRY_RUN=1)"
  set_step systemd "{\"ok\": true, \"dry_run\": true, \"dispatcher_port\": ${DISPATCHER_PORT}}"
else
  log "step 4/5 — systemd"
  mkdir -p "$SYSTEMD_DIR"
  PY_BIN=$(command -v python3)

  cat > "$SYSTEMD_DIR/disco-bus-dispatcher.service" <<EOF
[Unit]
Description=Disco-Bus Dispatcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PY_BIN $REPO_DIR/dispatcher/dispatcher.py
WorkingDirectory=$REPO_DIR/dispatcher
Restart=on-failure
RestartSec=5
Environment=DISCOBUS_PORT=$DISPATCHER_PORT
Environment=DISCOBUS_AGENTS_FILE=$CONFIG_DIR/agents.json
Environment=DISCOBUS_DB=$CONFIG_DIR/disco-bus.sqlite
Environment=DISCOBUS_MAX_BODY_BYTES=$MAX_BODY_BYTES
Environment=DISCOBUS_DISCORD_TOKEN_FILE=$CONFIG_DIR/discord-token
Environment=DISCOBUS_CHANNELS=$CONFIG_DIR/discord-channels.json
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

  cat > "$SYSTEMD_DIR/disco-bus-listener@.service" <<EOF
[Unit]
Description=Disco-Bus listener for agent %i
After=network-online.target disco-bus-dispatcher.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_DIR/listener-%i.env
Environment=DISCOBUS_AGENTS_FILE=$CONFIG_DIR/agents.json
Environment=DISCOBUS_INBOX=$CONFIG_DIR/inbox
Environment=DISCOBUS_DISPATCHER=http://127.0.0.1:$DISPATCHER_PORT
ExecStart=$PY_BIN $REPO_DIR/listeners/listener.py
WorkingDirectory=$REPO_DIR/listeners
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload

  if ! systemctl --user enable --now disco-bus-dispatcher.service >/dev/null 2>&1; then
    set_step systemd "{\"ok\": false, \"error\": \"dispatcher failed to start on port $DISPATCHER_PORT\"}"
    emit false "dispatcher failed to start on port $DISPATCHER_PORT"
  fi

  failed=""
  for a in $ALL_AGENTS; do
    systemctl --user enable --now "disco-bus-listener@${a}.service" >/dev/null 2>&1 \
      || failed="$failed $a"
  done

  if [ -n "$failed" ]; then
    set_step systemd "{\"ok\": false, \"dispatcher_port\": $DISPATCHER_PORT, \"failed_listeners\": \"${failed# }\"}"
    emit false "listeners failed to start:$failed"
  fi

  set_step systemd "{\"ok\": true, \"dispatcher_port\": $DISPATCHER_PORT}"
fi

# ─── step 5/5: smoke test ─────────────────────────────────────────────

if [ "$SMOKE_ENABLED" != "1" ] || [ "$DRY_RUN" = "1" ]; then
  log "step 5/5 — smoke test (skipped)"
  set_step smoke_test '{"ok": true, "skipped": true}'
  emit true
fi

log "step 5/5 — smoke test"

agents_arr=($ALL_AGENTS)
FROM="${SMOKE_FROM:-${agents_arr[0]}}"
TO="${SMOKE_TO:-${agents_arr[1]:-${agents_arr[0]}}}"

sleep 1
ping_resp=$(curl -s -m 5 -X POST "http://127.0.0.1:$DISPATCHER_PORT/mesh/ping" \
  -H 'Content-Type: application/json' \
  -d "{\"mesh_version\":\"0.5\",\"from\":\"$FROM\",\"to\":\"$TO\",\"subject\":\"robot-install-test\",\"body\":{\"installer\":\"robot-install.sh\"}}")

if ! echo "$ping_resp" | python3 -c 'import sys,json;sys.exit(0 if json.load(sys.stdin).get("state")=="SENT" else 1)' 2>/dev/null; then
  set_step smoke_test "{\"ok\": false, \"error\": \"dispatcher rejected test ping\"}"
  emit false "smoke test: dispatcher rejected ping: $ping_resp"
fi

msg_id=$(echo "$ping_resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
sleep 2
final_state=$(curl -s "http://127.0.0.1:$DISPATCHER_PORT/mesh/state/$msg_id" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("state","UNKNOWN"))')

if [ "$final_state" = "DELIVERED" ]; then
  set_step smoke_test "{\"ok\": true, \"msg_id\": $msg_id, \"from\": \"$FROM\", \"to\": \"$TO\", \"final_state\": \"DELIVERED\"}"
  emit true
else
  set_step smoke_test "{\"ok\": false, \"msg_id\": $msg_id, \"final_state\": \"$final_state\"}"
  emit false "smoke test: message #$msg_id state=$final_state (expected DELIVERED)"
fi
