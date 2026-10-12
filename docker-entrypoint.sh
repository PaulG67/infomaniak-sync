#!/bin/sh
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"

if ! getent group "${PGID}" >/dev/null 2>&1; then
  groupadd -g "${PGID}" iksync || true
fi
if ! getent passwd "${PUID}" >/dev/null 2>&1; then
  useradd -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin iksync || true
fi

mkdir -p /config/logs /config/filters /config/bisync /config/versions /shares
owner="$(stat -c %u /config 2>/dev/null || echo "")"
if [ "$owner" != "$PUID" ]; then
  chown -R "${PUID}:${PGID}" /config
fi

# Unraid rclone plugin: /boot/config/plugins/rclone/.rclone.conf, Eintrag [Infomaniak].
# Die Datei liegt oft als root:root 600. Vor dem Wechsel auf PUID eine lesbare Kopie anlegen.
SRC="${RCLONE_CONFIG_FILE:-/rclone/rclone.conf}"
DEST="/config/rclone-external.conf"
if [ -n "$SRC" ] && [ "$SRC" != "$DEST" ] && [ -f "$SRC" ]; then
  cp "$SRC" "$DEST"
  chown "${PUID}:${PGID}" "$DEST"
  chmod 600 "$DEST"
elif [ ! -f "$SRC" ]; then
  rm -f "$DEST"
fi
export RCLONE_CONFIG_FILE="$DEST"

exec gosu "${PUID}:${PGID}" \
  gunicorn "app.main:app" \
    --bind "0.0.0.0:${PORT:-8794}" \
    --workers 1 \
    --threads 8 \
    --timeout 180 \
    --access-logfile - \
    --error-logfile -
