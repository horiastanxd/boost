#!/usr/bin/env bash
# Boost installer - copies scripts to system paths and enables systemd services

set -e

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF_FILE="/etc/boost-auto.conf"

set_config_value() {
    local key="$1" value="$2"
    if grep -qE "^[[:space:]]*${key}=" "$CONF_FILE"; then
        sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${value}|" "$CONF_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$CONF_FILE"
    fi
}

migrate_config() {
    local key value backup
    backup="/etc/boost-auto.conf.backup-$(date +%Y%m%d-%H%M%S)"
    cp "$CONF_FILE" "$backup"
    install -m 644 "$REPO_DIR/config/boost-auto.conf" "$CONF_FILE"
    for key in \
        AUTO_MODE QUIET_HOURS_START QUIET_HOURS_END ALLOW_CRITICAL_AUTO \
        SUMMER_SILENT_NIGHTS AMBIENT_TEMP_C AMBIENT_TEMP_FILE \
        TEMP_CRITICAL TEMP_HOT BOOST_TEMP_LIMIT LOAD_HIGH LOAD_HIGH_DURATION \
        LOAD_IDLE LOAD_IDLE_DURATION PROMPT_COOLDOWN POLL_INTERVAL STATS_INTERVAL \
        AC_PROFILE BATTERY_PROFILE BATTERY_LOW_PCT BATTERY_CRITICAL_PCT BATTERY_LOW_NOTIFY \
        BOOST_EPP BOOST_PL1_PCT BOOST_PL2_PCT \
        SCREEN_LOCK_POWERSAVE BATTERY_CHARGE_LIMIT \
        SLOW_CHARGE_THRESHOLD_W SLOW_CHARGE_BATTERY_PCT SLOW_CHARGE_RECOVERY_PCT \
        GPU_PL_BOOST_W GPU_PL_POWERSAVE_W GPU_PL_SILENT_W
    do
        value=$(awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1); found=1} END {exit found ? 0 : 1}' "$backup" 2>/dev/null || true)
        [[ -n "$value" ]] && set_config_value "$key" "$value"
    done
    echo "  -> refreshed /etc/boost-auto.conf comments (backup: $backup)"
}

echo "[install] Copying scripts to /usr/local/bin..."
for bin in boost powersave silent summer restore power-save-originals auto power-report boost-web ac-event boost-statusbar; do
    install -m 755 "$REPO_DIR/bin/$bin" /usr/local/bin/"$bin"
    echo "  -> /usr/local/bin/$bin"
done

echo "[install] Copying lib to /usr/local/lib..."
install -m 644 "$REPO_DIR/lib/power-common.sh" /usr/local/lib/power-common.sh
install -m 644 "$REPO_DIR/lib/boost-web.py" /usr/local/lib/boost-web.py
install -m 755 "$REPO_DIR/lib/boost-daemon.py" /usr/local/lib/boost-daemon.py
install -m 755 "$REPO_DIR/lib/sensors.py" /usr/local/lib/sensors.py
install -m 755 "$REPO_DIR/lib/fancontrol.py" /usr/local/lib/fancontrol.py
# Platform layer: boost-web.py imports these at startup.
install -m 644 "$REPO_DIR/lib/boost_paths.py" /usr/local/lib/boost_paths.py
install -m 644 "$REPO_DIR/lib/platform_backend.py" /usr/local/lib/platform_backend.py
install -m 644 "$REPO_DIR/lib/platform_windows.py" /usr/local/lib/platform_windows.py
install -m 755 "$REPO_DIR/lib/boost-tray.py" /usr/local/bin/boost-tray

echo "[install] Copying dashboard assets to /usr/local/lib/webui..."
mkdir -p /usr/local/lib/webui
for asset in index.html app.css app.js; do
    install -m 644 "$REPO_DIR/lib/webui/$asset" /usr/local/lib/webui/"$asset"
    echo "  -> /usr/local/lib/webui/$asset"
done

echo "[install] Installing canonical mode presets..."
mkdir -p /usr/local/share/boost
install -m 644 "$REPO_DIR/config/presets.json" /usr/local/share/boost/presets.json
echo "  -> /usr/local/share/boost/presets.json"

echo "[install] Copying shell autocompletions..."
mkdir -p /usr/share/bash-completion/completions
install -m 644 "$REPO_DIR/packaging/linux/boost-completion.bash" /usr/share/bash-completion/completions/auto
for cmd in boost powersave silent restore summer; do
    ln -sf auto /usr/share/bash-completion/completions/"$cmd"
done

echo "[install] Installing desktop app launcher & tray autostart..."
mkdir -p /usr/local/share/applications
mkdir -p /etc/xdg/autostart
install -m 644 "$REPO_DIR/packaging/linux/desktop/boost-dashboard.desktop" /usr/local/share/applications/boost-dashboard.desktop
install -m 644 "$REPO_DIR/packaging/linux/desktop/boost-tray.desktop" /etc/xdg/autostart/boost-tray.desktop
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/local/share/applications >/dev/null 2>&1 || true
fi

echo "[install] Loading component temperature drivers..."
# DDR5 DIMM and SATA temperatures need drivers that are not autoloaded on most
# distributions. Failures are expected on hardware without them.
install -m 644 /dev/stdin /etc/modules-load.d/boost.conf <<'MODEOF'
# Loaded by Boost so RAM (DDR5 SPD hub) and SATA drive temperatures show up
# in /sys/class/hwmon. Harmless on machines without that hardware.
spd5118
drivetemp
MODEOF
for module in spd5118 drivetemp; do
    if modprobe "$module" 2>/dev/null; then
        echo "  -> loaded $module"
    else
        echo "  -> $module unavailable on this kernel/hardware (skipped)"
    fi
done

echo "[install] Installing systemd services..."
install -m 644 "$REPO_DIR/packaging/linux/systemd/power-save-originals.service" \
    /etc/systemd/system/power-save-originals.service
install -m 644 "$REPO_DIR/packaging/linux/systemd/boost-auto.service" \
    /etc/systemd/system/boost-auto.service
install -m 644 "$REPO_DIR/packaging/linux/systemd/boost-web.service" \
    /etc/systemd/system/boost-web.service
install -m 644 "$REPO_DIR/packaging/linux/systemd/boost-ac-init.service" \
    /etc/systemd/system/boost-ac-init.service
echo "[install] Installing suspend/resume hook..."
mkdir -p /usr/lib/systemd/system-sleep
install -m 755 "$REPO_DIR/packaging/linux/systemd/boost-sleep-hook" /usr/lib/systemd/system-sleep/boost
systemctl daemon-reload
systemctl enable power-save-originals.service
systemctl enable boost-ac-init.service
systemctl enable --now boost-web.service
echo "  -> Web dashboard enabled and started: http://127.0.0.1:8765"

echo "[install] Installing udev rules..."
install -m 644 "$REPO_DIR/packaging/linux/systemd/99-boost-power.rules" \
    /etc/udev/rules.d/99-boost-power.rules
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules
    udevadm trigger
fi

echo "[install] Installing default config..."
if [[ ! -f "$CONF_FILE" ]]; then
    install -m 644 "$REPO_DIR/config/boost-auto.conf" "$CONF_FILE"
    echo "  -> /etc/boost-auto.conf (edit to tune thresholds)"
else
    migrate_config
fi

mkdir -p /var/lib/power-profile

echo ""
echo "[install] Done. Commands available:"
echo "  boost | powersave | silent | summer | restore    — main shortcuts"
echo "  auto start | stop | status | logs       — intelligent auto-daemon"
echo "  auto stats | auto report                — text and web statistics"
echo "  auto web                                — local web dashboard"
echo "  auto sensors                            — every component temperature"
echo "  auto fans status|on|off|calibrate       — smart fan curves"
echo "  auto gpu-limit <watts>                  — GPU power limit"
echo ""
echo "Run 'powersave' now to start saving power."
echo "Run 'auto start' to enable automatic switching."

# Detect real user (sudo or pkexec)
REAL_USER=""
if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
    REAL_USER="$SUDO_USER"
elif [ -n "$PKEXEC_UID" ]; then
    REAL_USER=$(id -nu "$PKEXEC_UID")
fi

if [ -n "$REAL_USER" ]; then
    echo ""
    echo "[install] Starting tray applet for user $REAL_USER..."
    pkill -f boost-tray || true
    sudo -u "$REAL_USER" env "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u "$REAL_USER")/bus" "DISPLAY=${DISPLAY:-:0}" "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0}" "XDG_RUNTIME_DIR=/run/user/$(id -u "$REAL_USER")" nohup /usr/local/bin/boost-tray >/dev/null 2>&1 &
fi
