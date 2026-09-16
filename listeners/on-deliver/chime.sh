#!/usr/bin/env bash
# Disco-Bus on-deliver bell (Linux). Set in the listener env file:
#   DISCOBUS_ON_DELIVER=/path/to/listeners/on-deliver/chime.sh
# Envelope JSON arrives on stdin. v0.16: it goes through chime-policy.py
# first (skip list, cooldown, wake marker — see that file); exit 3 from the
# policy means "silent by policy" and is passed up so the listener logs it
# as such. No python3 = no policy = ring, as v0.15 did. Otherwise exit 0
# whether or not audio is available -- a missing speaker must never look
# like a lost letter.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  python3 "$HERE/chime-policy.py"
  rc=$?
  [ "$rc" -eq 3 ] && exit 3
else
  cat >/dev/null
fi
SOUND="${DISCOBUS_CHIME_SOUND:-/usr/share/sounds/freedesktop/stereo/message-new-instant.oga}"
if command -v paplay >/dev/null 2>&1 && [ -r "$SOUND" ]; then
  paplay "$SOUND" >/dev/null 2>&1 || true
elif command -v canberra-gtk-play >/dev/null 2>&1; then
  canberra-gtk-play -i message-new-instant >/dev/null 2>&1 || true
fi
exit 0
