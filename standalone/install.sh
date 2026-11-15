#!/usr/bin/env bash
# Installiert die Bambu Bridge als systemd-Dienst (Debian/Ubuntu LXC, als root ausführen)
set -euo pipefail
DIR=/opt/bambu-bridge
SRC="$(cd "$(dirname "$0")" && pwd)"

apt-get update -qq
apt-get install -y -qq python3 python3-venv ffmpeg >/dev/null   # ffmpeg: Kamera X1/H2/P2

id bambu &>/dev/null || useradd -r -s /usr/sbin/nologin bambu
mkdir -p "$DIR"
cp "$SRC/../bambu_bridge/bridge.py" "$SRC/requirements.txt" "$DIR/"
[ -f "$DIR/config.yaml" ] || cp "$SRC/config.example.yaml" "$DIR/config.yaml"

python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

chown -R root:bambu "$DIR"
chmod 640 "$DIR/config.yaml"

cp "$SRC/bambu-bridge.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable bambu-bridge >/dev/null

echo
echo "Installiert. Jetzt Konfiguration anpassen:"
echo "  nano $DIR/config.yaml"
echo "Dann starten und Log ansehen:"
echo "  systemctl restart bambu-bridge && journalctl -u bambu-bridge -f"
