#!/bin/bash
# Install / update Infomaniak Sync Unraid user template + pull image
# Run on Unraid Terminal:
# bash <(curl -fsSL https://raw.githubusercontent.com/PaulG67/infomaniak-sync/main/unraid/install-template.sh)

set -euo pipefail

TEMPLATE_DIR="/boot/config/plugins/dockerMan/templates-user"
USER_XML="${TEMPLATE_DIR}/my-infomaniak-sync.xml"
IMAGE="ghcr.io/paulg67/infomaniak-sync:latest"
RAW_BASE="https://raw.githubusercontent.com/PaulG67/infomaniak-sync/main"
SRC_DIR="/mnt/user/appdata/infomaniak-sync-src"
REPO="https://github.com/PaulG67/infomaniak-sync.git"

echo "==> 1/3 Unraid-Vorlage"
mkdir -p "${TEMPLATE_DIR}" /mnt/user/appdata/infomaniak-sync
curl -fsSL -H "Cache-Control: no-cache" "${RAW_BASE}/unraid/my-infomaniak-sync.xml?$(date +%s)" -o "${USER_XML}"

if ! grep -q '<Repository>ghcr.io/paulg67/infomaniak-sync:latest</Repository>' "${USER_XML}"; then
  echo "FEHLER: Vorlage enthaelt kein Repository — Abbruch."
  exit 1
fi
if ! grep -q 'Name="Shares"' "${USER_XML}"; then
  echo "FEHLER: Vorlage enthaelt keinen Shares-Pfad — Abbruch."
  exit 1
fi
if ! grep -q 'Name="Config"' "${USER_XML}"; then
  echo "FEHLER: Vorlage enthaelt keinen Config-Pfad — Abbruch."
  exit 1
fi
if ! grep -q 'Name="kDrive-ID"' "${USER_XML}"; then
  echo "FEHLER: Vorlage enthaelt keine kDrive-ID — Abbruch."
  exit 1
fi

echo "  ${USER_XML}"
wc -c "${USER_XML}" | awk '{print "  Groesse:" $1 " Bytes"}'

echo "==> 2/3 Docker-Image"
if docker pull "${IMAGE}" 2>/dev/null; then
  echo "  pulled ${IMAGE}"
else
  echo "  pull failed — building locally from GitHub"
  if command -v git >/dev/null 2>&1; then
    if [[ -d "${SRC_DIR}/.git" ]]; then
      git -C "${SRC_DIR}" pull --ff-only || true
    else
      rm -rf "${SRC_DIR}"
      git clone --depth 1 "${REPO}" "${SRC_DIR}"
    fi
  else
    mkdir -p "${SRC_DIR}"
    curl -fsSL "https://codeload.github.com/PaulG67/infomaniak-sync/tar.gz/refs/heads/main" \
      | tar -xz -C "${SRC_DIR}" --strip-components=1
  fi
  docker build -t "${IMAGE}" "${SRC_DIR}"
  echo "  tagged ${IMAGE}"
fi

echo "==> 3/3 Fertig"
echo
echo "Seite neu laden (F5), dann:"
echo "  Docker -> Container hinzufuegen -> Template infomaniak-sync"
echo "  Shares bleibt auf /mnt/user, Config auf /mnt/user/appdata/infomaniak-sync"
echo "  kDrive-ID, E-Mail und Anwendungspasswort optional hier oder spaeter in der WebUI"
echo "  WebUI: http://UNRAID-IP:8794"
echo
echo "Update: dieses Script erneut, danach Docker -> Force Update."
echo "Neue Felder: zusaetzlich Edit -> Apply."
