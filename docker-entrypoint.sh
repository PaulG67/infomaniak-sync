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

# Der Prozess läuft als PUID, nicht als root. HOME=/root ist dann nicht lesbar,
# und rclone version bricht ab, bevor es eine Version ausgibt.
export HOME=/tmp
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

touch /tmp/rclone-probe.conf
chmod 644 /tmp/rclone-probe.conf
if [ ! -x /usr/bin/rclone ]; then
  echo "rclone fehlt unter /usr/bin/rclone" >&2
elif command -v timeout >/dev/null 2>&1; then
  timeout 8 /usr/bin/rclone version --config /tmp/rclone-probe.conf > /tmp/rclone-probe.txt 2>/tmp/rclone-probe.err || true
  if [ -s /tmp/rclone-probe.txt ]; then
    head -n 1 /tmp/rclone-probe.txt
  else
    echo "rclone version ist fehlgeschlagen" >&2
  fi
fi

exec gosu "${PUID}:${PGID}" \
  gunicorn "app.main:app" \
    --bind "0.0.0.0:${PORT:-8794}" \
    --workers 1 \
    --threads 8 \
    --timeout 180 \
    --access-logfile - \
    --error-logfile -
