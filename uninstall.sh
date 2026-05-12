#!/usr/bin/env bash
# Disco-Bus uninstaller — removes systemd services and (optionally) all local data.
#
# What this does:
#   1. Stops + disables + removes systemd user services
#   2. Optionally removes ~/.disco-bus/ (database, inbox, configs, Discord token)
#   3. Optionally removes ~/.config/disco-bus/ (per-agent env files)
#
# What this does NOT do:
#   - Remove the cloned source repo. That stays.
#   - Touch anything outside the user's home directory.

set -uo pipefail

CONFIG_DIR="${HOME}/.disco-bus"
ENV_DIR="${HOME}/.config/disco-bus"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
note()   { printf '  %s\n' "$*"; }
warn()   { printf '\033[33m  warn:\033[0m %s\n' "$*"; }
ok()     { printf '\033[32m  ok:\033[0m %s\n' "$*"; }
section(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

confirm() {
  # confirm "Prompt text? [y/N]"
  local prompt="$1"
  local reply
  read -r -p "  $prompt " reply
  [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# ─── show what will happen ──────────────────────────────────────────

section "Disco-Bus uninstaller"

cat <<EOF
  This will stop and remove the Disco-Bus systemd user services.
  You will be asked separately before any data files are deleted.

  Services: disco-bus-dispatcher.service, disco-bus-listener@<agent>.service (each)
  System units dir: ${SYSTEMD_DIR}
EOF

confirm "Proceed with stopping services? [y/N]" || { note "aborted, nothing changed"; exit 0; }

# ─── stop + disable + remove units ──────────────────────────────────

section "Stopping services"

agents=()
if [ -f "${CONFIG_DIR}/agents.json" ]; then
  mapfile -t agents < <(python3 -c "import json; print('\n'.join(json.load(open('${CONFIG_DIR}/agents.json')).keys()))" 2>/dev/null)
fi

# Stop / disable per-agent listeners
for a in "${agents[@]}"; do
  if systemctl --user list-unit-files "disco-bus-listener@${a}.service" >/dev/null 2>&1; then
    systemctl --user stop "disco-bus-listener@${a}.service" 2>/dev/null && ok "stopped listener '$a'" || warn "could not stop listener '$a'"
    systemctl --user disable "disco-bus-listener@${a}.service" 2>/dev/null
  fi
done

# Also catch any listener instances not in agents.json (orphans)
mapfile -t orphan_units < <(systemctl --user list-units --type=service --all 2>/dev/null | grep -oE "disco-bus-listener@[^.]+\.service" | sort -u || true)
for unit in "${orphan_units[@]:-}"; do
  [ -z "$unit" ] && continue
  agent_name=${unit#disco-bus-listener@}
  agent_name=${agent_name%.service}
  if [[ ! " ${agents[*]:-} " =~ " ${agent_name} " ]]; then
    systemctl --user stop "$unit" 2>/dev/null && ok "stopped orphan listener '$agent_name'" || true
    systemctl --user disable "$unit" 2>/dev/null
  fi
done

# Stop dispatcher
if systemctl --user list-unit-files disco-bus-dispatcher.service >/dev/null 2>&1; then
  systemctl --user stop disco-bus-dispatcher.service 2>/dev/null && ok "stopped dispatcher" || warn "could not stop dispatcher"
  systemctl --user disable disco-bus-dispatcher.service 2>/dev/null
fi

# Remove unit files
removed=0
for unit_file in "${SYSTEMD_DIR}/disco-bus-dispatcher.service" "${SYSTEMD_DIR}/disco-bus-listener@.service"; do
  if [ -f "$unit_file" ]; then
    rm -f "$unit_file"
    ok "removed $(basename "$unit_file")"
    removed=1
  fi
done

if [ "$removed" -eq 1 ]; then
  systemctl --user daemon-reload
  ok "daemon-reload"
fi

# ─── ask about data ─────────────────────────────────────────────────

section "Data files"

if [ -d "$CONFIG_DIR" ]; then
  echo "  ${CONFIG_DIR} contains:"
  ls -la "$CONFIG_DIR" 2>/dev/null | sed 's/^/    /'
  echo ""
  if confirm "Delete ${CONFIG_DIR} (database, inbox, configs, Discord token)? [y/N]"; then
    rm -rf "$CONFIG_DIR"
    ok "removed $CONFIG_DIR"
  else
    note "kept $CONFIG_DIR — your messages and configs are preserved"
  fi
else
  note "no $CONFIG_DIR to remove"
fi

if [ -d "$ENV_DIR" ]; then
  echo ""
  echo "  ${ENV_DIR} contains:"
  ls -la "$ENV_DIR" 2>/dev/null | sed 's/^/    /'
  echo ""
  if confirm "Delete ${ENV_DIR} (per-agent env files)? [y/N]"; then
    rm -rf "$ENV_DIR"
    ok "removed $ENV_DIR"
  else
    note "kept $ENV_DIR"
  fi
fi

section "Done!"

cat <<EOF
  Disco-Bus services are removed.

  The source repo at $(dirname "$(readlink -f "${BASH_SOURCE[0]}")") is untouched.
  Delete it manually with: rm -rf <repo-path>

  To reinstall later: ./install.sh
EOF
