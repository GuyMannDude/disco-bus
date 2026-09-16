#!/usr/bin/env bash
# Disco-Bus on-deliver bell (Linux). Set in the listener env file:
#   DISCOBUS_ON_DELIVER=/path/to/listeners/on-deliver/chime.sh
# Envelope JSON arrives on stdin; we drain it and ring. Exit 0 whether or not
# audio is available -- a missing speaker must never look like a lost letter.
cat >/dev/null
SOUND="${DISCOBUS_CHIME_SOUND:-/usr/share/sounds/freedesktop/stereo/message-new-instant.oga}"
if command -v paplay >/dev/null 2>&1 && [ -r "$SOUND" ]; then
  paplay "$SOUND" >/dev/null 2>&1 || true
elif command -v canberra-gtk-play >/dev/null 2>&1; then
  canberra-gtk-play -i message-new-instant >/dev/null 2>&1 || true
fi
exit 0
