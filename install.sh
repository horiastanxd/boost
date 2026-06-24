#!/usr/bin/env bash
# Boost installer - copies scripts to system paths and enables systemd services

set -e

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[install] Copying scripts to /usr/local/bin..."
for bin in boost powersave silent restore power-save-originals auto power-report boost-web; do
    install -m 755 "$REPO_DIR/bin/$bin" /usr/local/bin/"$bin"
    echo "  -> /usr/local/bin/$bin"
done

echo "[install] Copying lib to /usr/local/lib..."
install -m 644 "$REPO_DIR/lib/power-common.sh" /usr/local/lib/power-common.sh
install -m 644 "$REPO_DIR/lib/boost-web.py" /usr/local/lib/boost-web.py

echo "[install] Installing systemd services..."
install -m 644 "$REPO_DIR/systemd/power-save-originals.service" \
    /etc/systemd/system/power-save-originals.service
install -m 644 "$REPO_DIR/systemd/boost-auto.service" \
    /etc/systemd/system/boost-auto.service
install -m 644 "$REPO_DIR/systemd/boost-web.service" \
    /etc/systemd/system/boost-web.service
systemctl daemon-reload
systemctl enable power-save-originals.service

echo "[install] Installing default config..."
if [[ ! -f /etc/boost-auto.conf ]]; then
    install -m 644 "$REPO_DIR/boost-auto.conf" /etc/boost-auto.conf
    echo "  -> /etc/boost-auto.conf (edit to tune thresholds)"
else
    echo "  -> /etc/boost-auto.conf already exists, skipping"
fi

mkdir -p /var/lib/power-profile

echo ""
echo "[install] Done. Commands available:"
echo "  boost | powersave | silent | restore    — manual profiles"
echo "  auto start | stop | status | logs       — intelligent auto-daemon"
echo "  auto stats | auto report                — text and web statistics"
echo "  auto web                                — local web dashboard"
echo ""
echo "Run 'powersave' now to start saving power."
echo "Run 'auto start' to enable automatic switching."
