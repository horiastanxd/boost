#!/usr/bin/env bash
# Boost installer - copies scripts to system paths and enables systemd service

set -e

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[install] Copying scripts to /usr/local/bin..."
for bin in boost powersave silent restore power-save-originals; do
    install -m 755 "$REPO_DIR/bin/$bin" /usr/local/bin/"$bin"
    echo "  -> /usr/local/bin/$bin"
done

echo "[install] Copying lib to /usr/local/lib..."
install -m 644 "$REPO_DIR/lib/power-common.sh" /usr/local/lib/power-common.sh

echo "[install] Installing systemd service..."
install -m 644 "$REPO_DIR/systemd/power-save-originals.service" \
    /etc/systemd/system/power-save-originals.service
systemctl daemon-reload
systemctl enable power-save-originals.service

mkdir -p /var/lib/power-profile

echo ""
echo "[install] Done. Commands available: boost | powersave | silent | restore"
echo "          Run 'powersave' now to start saving power."
