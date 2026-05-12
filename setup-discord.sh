#!/usr/bin/env bash
# Disco-Bus Discord mirror setup — adds Discord notifications to an existing install.
#
# What this does:
#   1. Walks you through creating a Discord bot (or uses one you already have)
#   2. Saves the bot token securely to ~/.disco-bus/discord-token
#   3. Asks for channel IDs (global firehose + per-agent channels, all optional)
#   4. Writes ~/.disco-bus/discord-channels.json
#   5. Restarts the dispatcher so it picks up the new config
#   6. Sends a test ping; you check Discord to see it arrive
#
# Prerequisites: install.sh has already been run.
# This script is safe to re-run if you want to change channel IDs.

set -uo pipefail

CONFIG_DIR="${HOME}/.disco-bus"

bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
note()   { printf '  %s\n' "$*"; }
warn()   { printf '\033[33m  warn:\033[0m %s\n' "$*"; }
err()    { printf '\033[31m  error:\033[0m %s\n' "$*" >&2; }
ok()     { printf '\033[32m  ok:\033[0m %s\n' "$*"; }
section(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

die() { err "$*"; exit 1; }

# ─── prereqs ─────────────────────────────────────────────────────────

[ -f "${CONFIG_DIR}/agents.json" ] || die "no agents.json found at ${CONFIG_DIR}. Run ./install.sh first."
command -v curl >/dev/null 2>&1 || die "curl is required"

mapfile -t agents < <(python3 -c "import json; print('\n'.join(json.load(open('${CONFIG_DIR}/agents.json')).keys()))")
[ ${#agents[@]} -gt 0 ] || die "no agents in registry"

# ─── 1. walk through Discord bot creation ───────────────────────────

section "Step 1/4 — Discord bot"

cat <<'EOF'
  You need a Discord bot account with permission to post in your server.
  If you already have one, paste its token below.

  If not, here is the 60-second setup. You only do this ONCE.

  1. Open https://discord.com/developers/applications in your browser.
  2. Click "New Application" (top right). Name it anything.
  3. Click into the application, then "Bot" in the left sidebar.
  4. Click "Reset Token" → "Yes, do it" → click "Copy".
     (The token is a secret, like a password. Do not share it.)
  5. Go to "OAuth2" → "URL Generator" in the left sidebar.
     - Under "Scopes", check: bot
     - Under "Bot Permissions", check: Send Messages
     - Copy the URL at the bottom and open it in a new tab.
     - Pick the Discord server you want the bot to join. Click "Authorize".

  Now you have a bot account that can post in your Discord server.

EOF

read -r -s -p "  Paste the bot token (input is hidden, then press Enter): " token
echo ""
[ -n "${token// }" ] || die "no token entered"

mkdir -p "$CONFIG_DIR"
printf '%s' "$token" > "${CONFIG_DIR}/discord-token"
chmod 600 "${CONFIG_DIR}/discord-token"
ok "saved token to ${CONFIG_DIR}/discord-token (0600 perms)"

api_response=$(curl -s -m 10 -H "Authorization: Bot $token" "https://discord.com/api/v10/users/@me")
if echo "$api_response" | grep -q '"username"'; then
  bot_name=$(echo "$api_response" | python3 -c "import sys,json;print(json.load(sys.stdin).get('username','?'))")
  ok "Discord says this bot is: $bot_name"
else
  warn "Discord did not accept the token. Response: ${api_response:0:200}"
  warn "Double-check you copied the token correctly, then re-run."
  exit 1
fi

# ─── 2. ask for channel IDs ─────────────────────────────────────────

section "Step 2/4 — Channel IDs"

cat <<'EOF'
  Channel IDs are big numbers from Discord. To get one:

  1. In Discord, click the gear icon next to your username (bottom left).
  2. Go to "Advanced" and turn ON "Developer Mode". Close settings.
  3. Right-click any channel → "Copy Channel ID".
     The clipboard now holds a number like 1234567890123456789.

  Press Enter to skip any channel you don't want.

EOF

validate_id() {
  # Returns 0 if input is a positive integer, 1 otherwise
  [[ "$1" =~ ^[0-9]+$ ]]
}

# Collect all channel IDs into a tempfile shell-safely, then hand off to python
TMP_CHANNELS=$(mktemp)
trap 'rm -f "$TMP_CHANNELS"' EXIT

read -r -p "  GLOBAL firehose channel ID (every envelope mirrored here, or Enter to skip): " global_id
if [ -n "${global_id// }" ]; then
  if validate_id "$global_id"; then
    echo "global $global_id" >> "$TMP_CHANNELS"
    ok "global firehose: $global_id"
  else
    warn "'$global_id' doesn't look like a Discord channel ID — skipping"
  fi
fi

for a in "${agents[@]}"; do
  read -r -p "  Channel ID for agent '$a' (or Enter to skip): " cid
  if [ -n "${cid// }" ]; then
    if validate_id "$cid"; then
      echo "agent $a $cid" >> "$TMP_CHANNELS"
      ok "agent '$a' -> $cid"
    else
      warn "'$cid' doesn't look like a Discord channel ID — skipping for $a"
    fi
  fi
done

python3 - "$TMP_CHANNELS" "$CONFIG_DIR/discord-channels.json" <<'PY'
import json, sys
data = {"agents": {}}
with open(sys.argv[1]) as f:
    for line in f:
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "global":
            data["global"] = int(parts[1])
        elif parts[0] == "agent":
            data["agents"][parts[1]] = int(parts[2])
if not data["agents"]:
    del data["agents"]
with open(sys.argv[2], "w") as f:
    json.dump(data, f, indent=2)
print(f"  ok: wrote {sys.argv[2]}")
PY

# ─── 3. restart dispatcher ──────────────────────────────────────────

section "Step 3/4 — Reloading dispatcher"

if systemctl --user is-active disco-bus-dispatcher.service >/dev/null 2>&1; then
  systemctl --user restart disco-bus-dispatcher.service
  ok "dispatcher restarted (so it reads the new Discord config)"
else
  warn "dispatcher service is not running — start it with: systemctl --user start disco-bus-dispatcher.service"
fi

# ─── 4. smoke test ──────────────────────────────────────────────────

section "Step 4/4 — Discord smoke test"

first="${agents[0]}"
second="${agents[1]:-$first}"
dispatcher_port=$(systemctl --user show disco-bus-dispatcher.service -p Environment --value 2>/dev/null | tr ' ' '\n' | grep '^DISCOBUS_PORT=' | cut -d= -f2)
dispatcher_port="${dispatcher_port:-9100}"

sleep 1
response=$(curl -s -m 5 -X POST "http://127.0.0.1:${dispatcher_port}/mesh/ping" \
  -H "Content-Type: application/json" \
  -d "{\"mesh_version\":\"0.5\",\"from\":\"${first}\",\"to\":\"${second}\",\"subject\":\"discord-test\",\"body\":{\"note\":\"If you see this in Discord, the mirror is working.\"}}")

if echo "$response" | grep -q '"state":"SENT"'; then
  msg_id=$(echo "$response" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
  ok "test ping #${msg_id} sent — check Discord now"
  note "(may take ~1 second to appear)"
else
  err "smoke test failed: $response"
fi

section "Done!"

cat <<EOF
  Discord mirror is configured.

  Token:       ${CONFIG_DIR}/discord-token (chmod 600)
  Channel map: ${CONFIG_DIR}/discord-channels.json

  Re-run this script any time to change channel IDs or rotate the token.
EOF
