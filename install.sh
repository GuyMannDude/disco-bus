#!/usr/bin/env bash
# Disco-Bus installer — interactive wizard for the bus-only setup.
#
# What this does:
#   1. Checks dependencies (python3 >= 3.10, node, npm, systemctl --user)
#   2. Asks which agents you want and auto-allocates listener ports
#   3. Writes ~/.disco-bus/agents.json + ~/.config/disco-bus/listener-*.env
#   4. npm install for the MCP server
#   5. Installs + starts user systemd services (dispatcher + per-agent listeners)
#   6. Smoke-tests the install with a real ping
#
# What this does NOT do:
#   - Discord setup. Run ./setup-discord.sh after this if you want Discord mirroring.
#   - Anything requiring sudo. Everything is user-mode.
#
# Re-runnable: safe to run again if you want to add more agents.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/.disco-bus"
ENV_DIR="${HOME}/.config/disco-bus"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
DISPATCHER_DEFAULT_PORT=9100
LISTENER_PORT_START=9131

bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
note()   { printf '  %s\n' "$*"; }
warn()   { printf '\033[33m  warn:\033[0m %s\n' "$*"; }
err()    { printf '\033[31m  error:\033[0m %s\n' "$*" >&2; }
ok()     { printf '\033[32m  ok:\033[0m %s\n' "$*"; }
section(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

die() {
  err "$*"
  exit 1
}

# ─── 1. dependency check ──────────────────────────────────────────────

section "Step 1/6 — Checking dependencies"

check_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "missing: $1 ($2)"
    return 1
  fi
  ok "$1 found"
}

missing=0
check_cmd python3 "Python 3.10+" || missing=1
check_cmd node    "Node.js (for MCP server)" || missing=1
check_cmd npm     "npm (for MCP server deps)" || missing=1
check_cmd curl    "for smoke test" || missing=1
check_cmd systemctl "user services" || missing=1
[ $missing -eq 0 ] || die "install the missing tools above, then re-run."

py_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
py_major=$(echo "$py_version" | cut -d. -f1)
py_minor=$(echo "$py_version" | cut -d. -f2)
if [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 10 ]; }; then
  die "python3 is $py_version — need 3.10 or newer"
fi
ok "python3 is $py_version"

if ! systemctl --user is-system-running >/dev/null 2>&1 && ! systemctl --user list-units >/dev/null 2>&1; then
  warn "systemctl --user does not appear to be working."
  warn "If you are on a server without a graphical session, you may need:"
  warn "  loginctl enable-linger $USER"
  warn "  then log out and back in"
  exit 1
fi

# ─── 2. ask for agent names ──────────────────────────────────────────

section "Step 2/6 — Configure agents"

existing_agents=()
if [ -f "${CONFIG_DIR}/agents.json" ]; then
  mapfile -t existing_agents < <(python3 -c "import json; print('\n'.join(json.load(open('${CONFIG_DIR}/agents.json')).keys()))" 2>/dev/null)
  if [ ${#existing_agents[@]} -gt 0 ]; then
    note "Existing agents: ${existing_agents[*]}"
    note "(You can add more — existing ones are preserved.)"
  fi
fi

while true; do
  if [ ${#existing_agents[@]} -gt 0 ]; then
    read -r -p "  Add more agents? Type names separated by spaces, or press Enter to skip: " new_agents_raw
  else
    read -r -p "  What agents do you want? Type names separated by spaces (e.g., alice bob): " new_agents_raw
  fi

  if [ -z "${new_agents_raw// }" ]; then
    if [ ${#existing_agents[@]} -gt 0 ]; then
      new_agents=()
      break
    fi
    note "You need at least one agent. Try something like: alpha beta"
    continue
  fi

  read -r -a new_agents <<< "$new_agents_raw"
  valid=1
  for a in "${new_agents[@]}"; do
    if ! [[ "$a" =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,30}$ ]]; then
      err "invalid agent name: '$a' (must start with a letter; letters, digits, _ and -; max 31 chars)"
      valid=0
      break
    fi
  done
  [ "$valid" -eq 1 ] && break
done

# ─── 3. allocate ports and write config ──────────────────────────────

section "Step 3/6 — Allocating ports + writing config"

port_in_use() {
  ss -tln 2>/dev/null | awk '{print $4}' | grep -qE ":$1\$"
}

# Pick dispatcher port
DISPATCHER_PORT=$DISPATCHER_DEFAULT_PORT
if port_in_use "$DISPATCHER_PORT" && [ ! -f "${CONFIG_DIR}/agents.json" ]; then
  warn "port $DISPATCHER_PORT is already in use"
  for p in 9101 9102 9103 9104 9105; do
    if ! port_in_use "$p"; then
      DISPATCHER_PORT=$p
      ok "using dispatcher port $p instead"
      break
    fi
  done
fi

mkdir -p "$CONFIG_DIR" "$ENV_DIR" "$SYSTEMD_DIR"

# Build / merge agents.json
python3 - "$CONFIG_DIR/agents.json" "$LISTENER_PORT_START" "${new_agents[@]}" <<'PY'
import json, sys, socket
from pathlib import Path

path = Path(sys.argv[1])
port_start = int(sys.argv[2])
new_agents = sys.argv[3:]

data = {}
if path.exists():
    data = json.loads(path.read_text())

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

next_port = port_start
for name in new_agents:
    if name in data:
        print(f"  ok: '{name}' already in registry — keeping existing config")
        continue
    while not port_free(next_port):
        next_port += 1
    url = f"http://127.0.0.1:{next_port}/inbox"
    data[name] = {"url": url}
    used_ports.add(next_port)
    print(f"  ok: '{name}' -> {url}")
    next_port += 1

path.write_text(json.dumps(data, indent=2))
print(f"  ok: wrote {path}")
PY

# Per-agent listener env files
all_agents=$(python3 -c "import json; print(' '.join(json.load(open('${CONFIG_DIR}/agents.json')).keys()))")
for a in $all_agents; do
  port=$(python3 -c "import json; url=json.load(open('${CONFIG_DIR}/agents.json'))['$a']['url']; print(url.rsplit(':',1)[-1].split('/')[0])")
  env_file="${ENV_DIR}/listener-${a}.env"
  if [ ! -f "$env_file" ]; then
    cat > "$env_file" <<EOF
DISCOBUS_AGENT=$a
DISCOBUS_PORT=$port
EOF
    ok "wrote $env_file"
  else
    note "kept existing $env_file"
  fi
done

# ─── 4. MCP npm install ──────────────────────────────────────────────

section "Step 4/6 — Installing MCP server dependencies"

if [ -d "${REPO_DIR}/mcp/node_modules" ]; then
  ok "mcp/node_modules already present, skipping npm install"
else
  ( cd "${REPO_DIR}/mcp" && npm install --silent ) || die "npm install failed"
  ok "npm install complete"
fi

# ─── 5. systemd units ────────────────────────────────────────────────

section "Step 5/6 — Installing systemd user services"

# Generate dispatcher unit with the chosen port + repo path
cat > "${SYSTEMD_DIR}/disco-bus-dispatcher.service" <<EOF
[Unit]
Description=Disco-Bus Dispatcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${REPO_DIR}/dispatcher/dispatcher.py
WorkingDirectory=${REPO_DIR}/dispatcher
Restart=on-failure
RestartSec=5
Environment=DISCOBUS_PORT=${DISPATCHER_PORT}
Environment=DISCOBUS_AGENTS_FILE=${CONFIG_DIR}/agents.json
Environment=DISCOBUS_DB=${CONFIG_DIR}/disco-bus.sqlite
Environment=DISCOBUS_DISCORD_TOKEN_FILE=${CONFIG_DIR}/discord-token
Environment=DISCOBUS_CHANNELS=${CONFIG_DIR}/discord-channels.json
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
ok "wrote ${SYSTEMD_DIR}/disco-bus-dispatcher.service (port ${DISPATCHER_PORT})"

cat > "${SYSTEMD_DIR}/disco-bus-listener@.service" <<EOF
[Unit]
Description=Disco-Bus listener for agent %i
After=network-online.target disco-bus-dispatcher.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${ENV_DIR}/listener-%i.env
Environment=DISCOBUS_AGENTS_FILE=${CONFIG_DIR}/agents.json
Environment=DISCOBUS_INBOX=${CONFIG_DIR}/inbox
Environment=DISCOBUS_DISPATCHER=http://127.0.0.1:${DISPATCHER_PORT}
ExecStart=/usr/bin/python3 ${REPO_DIR}/listeners/listener.py
WorkingDirectory=${REPO_DIR}/listeners
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
ok "wrote ${SYSTEMD_DIR}/disco-bus-listener@.service"

systemctl --user daemon-reload
systemctl --user enable --now disco-bus-dispatcher.service >/dev/null 2>&1 || die "failed to start dispatcher"
ok "dispatcher running"

for a in $all_agents; do
  systemctl --user enable --now "disco-bus-listener@${a}.service" >/dev/null 2>&1 \
    && ok "listener for '$a' running" \
    || warn "failed to start listener for '$a' — check 'journalctl --user -u disco-bus-listener@${a}.service'"
done

# ─── 6. smoke test ───────────────────────────────────────────────────

section "Step 6/6 — Smoke test"

sleep 1
first_agent=$(echo "$all_agents" | awk '{print $1}')
second_agent=$(echo "$all_agents" | awk '{print $2}')
[ -z "$second_agent" ] && second_agent="$first_agent"

response=$(curl -s -m 5 -X POST "http://127.0.0.1:${DISPATCHER_PORT}/mesh/ping" \
  -H "Content-Type: application/json" \
  -d "{\"mesh_version\":\"0.5\",\"from\":\"${first_agent}\",\"to\":\"${second_agent}\",\"subject\":\"install-test\",\"body\":{\"hello\":\"first message\"}}")

if echo "$response" | grep -q '"state":"SENT"'; then
  msg_id=$(echo "$response" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
  sleep 1
  state=$(curl -s "http://127.0.0.1:${DISPATCHER_PORT}/mesh/state/${msg_id}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('state','?'))")
  if [ "$state" = "DELIVERED" ]; then
    ok "test message #${msg_id} delivered end-to-end"
  else
    warn "test message accepted but state is '$state' — check 'journalctl --user -u disco-bus-listener@${second_agent}.service'"
  fi
else
  err "smoke test failed: $response"
  exit 1
fi

# ─── done ─────────────────────────────────────────────────────────────

section "Done!"

cat <<EOF
  Disco-Bus is live.

  Dispatcher:  http://127.0.0.1:${DISPATCHER_PORT}
  Config:      ${CONFIG_DIR}/agents.json
  Inboxes:     ${CONFIG_DIR}/inbox/<agent>/
  Logs:        journalctl --user -u disco-bus-dispatcher
               journalctl --user -u disco-bus-listener@<agent>

  Send a message:
    curl -X POST http://127.0.0.1:${DISPATCHER_PORT}/mesh/ping \\
      -H 'Content-Type: application/json' \\
      -d '{"mesh_version":"0.5","from":"<sender>","to":"<recipient>","subject":"hi","body":{"text":"hello"}}'

  See traffic:
    curl http://127.0.0.1:${DISPATCHER_PORT}/mesh/history | python3 -m json.tool

  Want Discord mirroring (so you can watch agent traffic in Discord channels)?
    ./setup-discord.sh

  To remove everything later:
    ./uninstall.sh
EOF
