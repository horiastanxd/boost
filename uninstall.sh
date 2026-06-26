#!/usr/bin/env bash
# Boost uninstaller - removes all Boost components and restores BIOS defaults

set -e

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

echo "[uninstall] Restoring BIOS power defaults first..."
/usr/local/bin/restore 2>/dev/null && echo "  -> Power defaults restored." || echo "  -> restore skipped (already uninstalled or not applied)."

echo ""
echo "[uninstall] Stopping and disabling services..."
for svc in boost-auto.service boost-web.service power-save-originals.service boost-ac-init.service; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
done

echo "[uninstall] Removing systemd units..."
rm -f /etc/systemd/system/boost-auto.service
rm -f /etc/systemd/system/boost-web.service
rm -f /etc/systemd/system/power-save-originals.service
rm -f /etc/systemd/system/boost-ac-init.service
systemctl daemon-reload

echo "[uninstall] Removing binaries..."
for bin in boost powersave silent summer restore power-save-originals auto power-report boost-web ac-event boost-tray; do
    rm -f "/usr/local/bin/${bin}"
    echo "  -> removed /usr/local/bin/${bin}"
done

echo "[uninstall] Removing libraries..."
rm -f /usr/local/lib/power-common.sh
rm -f /usr/local/lib/boost-web.py
rm -f /usr/local/lib/boost-daemon.py

echo "[uninstall] Removing shell completions..."
rm -f /usr/share/bash-completion/completions/auto
for cmd in boost powersave silent restore summer; do
    rm -f "/usr/share/bash-completion/completions/${cmd}"
done

echo "[uninstall] Removing desktop integration..."
rm -f /usr/local/share/applications/boost-dashboard.desktop
rm -f /etc/xdg/autostart/boost-tray.desktop
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database /usr/local/share/applications >/dev/null 2>&1 || true

echo "[uninstall] Removing udev rules..."
rm -f /etc/udev/rules.d/99-boost-power.rules
udevadm control --reload-rules 2>/dev/null || true

echo "[uninstall] Removing state directory..."
rm -rf /var/lib/power-profile

pkill -f boost-tray 2>/dev/null || true

printf '\n[uninstall] Remove /etc/boost-auto.conf? [y/N]: '
read -r CONFIRM
if [[ "${CONFIRM,,}" == "y" ]]; then
    rm -f /etc/boost-auto.conf
    echo "  -> Config removed."
else
    echo "  -> Config kept at /etc/boost-auto.conf"
fi

echo ""
echo "[uninstall] Done. Boost has been completely removed."
echo "           Run 'hash -r' or open a new terminal to clear shell cache."
