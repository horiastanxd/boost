# Troubleshooting Guide

## Quick Diagnostic

Run `auto doctor` for an automated health check:

```bash
sudo auto doctor
```

This checks: power-profiles-daemon, NVIDIA GPU, RAPL, CPU temperature sensor,
notification actions, statistics history, and report status.

## Common Issues

### "Command not found" after install

**Symptom:** Running `boost` or `powersave` returns "command not found".

**Cause:** `/usr/local/bin` is not in your PATH.

**Fix:**
```bash
# Add to ~/.bashrc or ~/.zshrc
export PATH="$PATH:/usr/local/bin"
# Then reload
source ~/.bashrc
```

### "Permission denied" when running commands

**Symptom:** Error about not being able to write to sysfs.

**Cause:** The command didn't auto-elevate. This can happen if `sudo` is not
configured for your user or if the script can't detect the non-root user.

**Fix:**
```bash
# Run explicitly with sudo
sudo boost
# Or check sudo configuration
sudo -v
```

### Web dashboard not loading

**Symptom:** `http://127.0.0.1:8765` shows "Connection refused" or doesn't load.

**Diagnostic steps:**

1. Check if the service is running:
   ```bash
   sudo systemctl status boost-web.service
   ```

2. Check for errors:
   ```bash
   sudo journalctl -u boost-web.service -n 30 --no-pager
   ```

3. Check if the port is in use:
   ```bash
   sudo ss -tlnp | grep 8765
   ```

4. Restart the service:
   ```bash
   sudo systemctl restart boost-web.service
   ```

5. If the port is occupied by another process, edit the systemd service to use a
   different port:
   ```bash
   sudo systemctl edit boost-web.service
   ```
   Add:
   ```
   [Service]
   ExecStart=
   ExecStart=/usr/local/bin/boost-web --host 127.0.0.1 --port 8766
   ```

### Auto daemon not running

**Symptom:** `auto status` shows "inactive" for the auto helper.

**Diagnostic steps:**

1. Check service status:
   ```bash
   sudo systemctl status boost-auto.service
   ```

2. View recent logs:
   ```bash
   sudo journalctl -u boost-auto.service -n 30 --no-pager
   ```

3. Start manually:
   ```bash
   sudo auto start
   ```

4. If it fails to start, check for Python errors:
   ```bash
   sudo python3 /usr/local/lib/boost-daemon.py
   ```
   (Run in foreground to see errors; Ctrl+C to stop.)

### No desktop notifications

**Symptom:** Auto mode suggestions don't appear as notifications.

**Diagnostic steps:**

1. Check if `notify-send` works:
   ```bash
   notify-send -u critical "Test" "This is a test notification"
   ```

2. Check if `notify-send` supports action buttons (required for interactive
   notifications):
   ```bash
   notify-send --help 2>/dev/null | grep -- '--action'
   ```
   If no output, your `notify-send` doesn't support action buttons. Notifications
   will still appear but without the interactive buttons.

3. If using Wayland, ensure `DBUS_SESSION_BUS_ADDRESS` is set correctly:
   ```bash
   echo $DBUS_SESSION_BUS_ADDRESS
   ```
   Should show something like `unix:path=/run/user/$UID/bus`.

4. If using GNOME, check "Do Not Disturb" mode — it suppresses notifications.

### System tray icon not showing

**Symptom:** `boost-tray` is running but no icon appears in the system tray.

**Diagnostic steps:**

1. Check if the tray applet is running:
   ```bash
   ps aux | grep boost-tray
   ```

2. Start it manually to see errors:
   ```bash
   /usr/local/bin/boost-tray
   ```

3. **GNOME 42+ users:** Install the AppIndicator extension:
   ```bash
   # Install Extension Manager first
   sudo apt install gnome-shell-extension-manager
   # Then open Extension Manager and install
   # "AppIndicator and KStatusNotifierItem Support"
   ```

4. Check if the required GTK libraries are installed:
   ```bash
   dpkg -l | grep ayatanaappindicator  # Debian/Ubuntu
   rpm -qa | grep ayatana              # Fedora/RHEL
   pacman -Q | grep ayatana            # Arch
   ```

5. Try running with `GDK_BACKEND=x11`:
   ```bash
   GDK_BACKEND=x11 /usr/local/bin/boost-tray
   ```

### Temperature sensor not detected

**Symptom:** CPU temperature shows 0°C in status or dashboard.

**Diagnostic steps:**

1. Check available sensors:
   ```bash
   # List all hwmon devices
   ls /sys/class/hwmon/
   # Check their names
   for hw in /sys/class/hwmon/hwmon*/name; do echo "$(cat $hw) - $hw"; done
   ```

2. Install lm-sensors and detect sensors:
   ```bash
   sudo apt install lm-sensors
   sudo sensors-detect --auto
   ```

3. Check if the kernel module is loaded:
   ```bash
   lsmod | grep -E 'coretemp|k10temp|zenpower'
   # Intel: coretemp should be loaded
   # AMD: k10temp should be loaded
   ```

4. If using a VM or container, temperature sensors are typically not available.

### GPU stats not showing

**Symptom:** GPU shows 0°C / 0W in status or dashboard.

**For NVIDIA GPUs:**

1. Check if NVIDIA driver is installed:
   ```bash
   nvidia-smi
   ```

2. If `nvidia-smi` is not found, install the NVIDIA driver:
   ```bash
   # Ubuntu/Debian
   sudo apt install nvidia-driver-550
   # Fedora/RHEL
   sudo dnf install akmod-nvidia
   # Arch
   sudo pacman -S nvidia
   ```

3. Reboot after driver installation.

**For AMD GPUs:**

1. Check if the `amdgpu` driver is loaded:
   ```bash
   lsmod | grep amdgpu
   ```

2. Check if power cap is available:
   ```bash
   # Find the amdgpu hwmon
   for card in /sys/class/drm/card*/; do
     for hw in "${card}device/hwmon/hwmon"*/; do
       if [[ -r "${hw}name" ]] && [[ "$(cat ${hw}name)" == "amdgpu" ]]; then
         echo "AMD GPU hwmon: $hw"
         cat "${hw}power1_cap" 2>/dev/null || echo "  power1_cap not available"
       fi
     done
   done
   ```

3. If `power1_cap` is missing, your AMD GPU or driver version may not support
   power limit control. This is common on older AMD GPUs or certain mobile
   variants.

### RAPL power limits not working

**Symptom:** PL1/PL2 show 0W or "RAPL path missing" in `auto doctor`.

**Diagnostic steps:**

1. Check if RAPL is available:
   ```bash
   ls /sys/class/powercap/intel-rapl/
   ```

2. If the directory doesn't exist:
   - You likely have an AMD CPU (AMD doesn't use Intel RAPL).
   - This is expected — Boost skips RAPL tuning on AMD systems.
   - CPU governor, EPP, turbo, and GPU limits still work.

3. If the directory exists but shows 0W:
   ```bash
   cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw
   ```
   If this returns 0, your system's BIOS may not expose RAPL limits, or the
   `intel_rapl` kernel module needs to be loaded:
   ```bash
   sudo modprobe intel_rapl
   ```

### "Summer mode" not working

**Symptom:** `summer on` doesn't seem to do anything.

**Diagnostic steps:**

1. Check if summer nights is enabled:
   ```bash
   auto status | grep "Summer"
   ```

2. Check quiet hours are set:
   ```bash
   auto status | grep "Quiet hours"
   ```
   If quiet hours are `00:00-00:00`, they're disabled. Set them:
   ```bash
   sudo auto quiet-hours 22:00 08:00
   ```

3. Summer nights only activates during quiet hours. It's not a separate profile —
   it auto-applies Silent mode during quiet hours when enabled.

### Stats file growing too large

**Symptom:** Disk space warning or slow web dashboard.

**Fix:** The stats file is automatically rotated at 250KB (keeps header + last
2000 lines). To manually reset:
```bash
sudo truncate -s 0 /var/lib/power-profile/stats.csv
# Or keep the header
sudo sh -c 'head -1 /var/lib/power-profile/stats.csv > /tmp/stats.csv && mv /tmp/stats.csv /var/lib/power-profile/stats.csv'
```

### Web dashboard shows "Waiting for telemetry data..."

**Symptom:** The chart area shows this message and never updates.

**Diagnostic steps:**

1. Check if the stats file exists and has data:
   ```bash
   ls -la /var/lib/power-profile/stats.csv
   wc -l /var/lib/power-profile/stats.csv
   ```

2. If the file doesn't exist or has only the header, the daemon hasn't recorded
   any samples yet. Wait up to 60 seconds for the first sample.

3. If the daemon is not running, start it:
   ```bash
   sudo auto start
   ```

### Profile changes not taking effect

**Symptom:** Running `boost` or `powersave` doesn't change system behavior.

**Diagnostic steps:**

1. Verify the profile was applied:
   ```bash
   boost --status
   ```

2. Check current power profile:
   ```bash
   powerprofilesctl get
   ```

3. Check if another power management tool is conflicting:
   ```bash
   systemctl status tlp  # TLP
   systemctl status auto-cpufreq  # auto-cpufreq
   ```
   If another tool is active, disable it:
   ```bash
   sudo systemctl stop tlp
   sudo systemctl disable tlp
   ```

4. Check sysfs values directly:
   ```bash
   cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
   cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
   ```

### "auto doctor" shows warnings

**Symptom:** `auto doctor` shows WARN for some items.

| Warning | Meaning | Action |
|---------|---------|--------|
| `GNOME power profile sync` | `powerprofilesctl` not found | Install `power-profiles-daemon` |
| `NVIDIA stats and power limit` | `nvidia-smi` not found or no GPU | Install NVIDIA driver or ignore if using AMD/Intel |
| `CPU power limits` | RAPL path missing | Expected on AMD CPUs; ignore |
| `CPU temperature sensor` | No temp sensor found | Install `lm-sensors` or check kernel modules |
| `Notification buttons` | `notify-send` lacks `--action` | Update `libnotify`; notifications still work |
| `Statistics history` | No stats recorded yet | Run `auto stats` or wait for daemon |
| `Latest web report` | No report generated | Run `auto report` |

## Logs

### Auto daemon logs
```bash
sudo journalctl -u boost-auto.service -f
```

### Web server logs
```bash
sudo journalctl -u boost-web.service -f
```

### All Boost-related logs
```bash
sudo journalctl -t power-profile -t boost-auto -t boost-web -f
```

## Reverting / Uninstalling

### Manual uninstall
```bash
# Stop services
sudo systemctl stop boost-auto.service boost-web.service power-save-originals.service

# Disable services
sudo systemctl disable boost-auto.service boost-web.service power-save-originals.service

# Remove binaries
sudo rm -f /usr/local/bin/{boost,powersave,silent,restore,summer,auto,power-report,boost-web,ac-event,power-save-originals,boost-tray}

# Remove libraries
sudo rm -f /usr/local/lib/power-common.sh /usr/local/lib/boost-web.py /usr/local/lib/boost-daemon.py

# Remove config and state
sudo rm -rf /etc/boost-auto.conf /var/lib/power-profile/

# Remove systemd services
sudo rm -f /etc/systemd/system/{boost-auto,boost-web,power-save-originals}.service
sudo systemctl daemon-reload

# Remove udev rules
sudo rm -f /etc/udev/rules.d/99-boost-power.rules
sudo udevadm control --reload-rules

# Remove bash completions
sudo rm -f /usr/share/bash-completion/completions/{auto,boost,powersave,silent,restore,summer}

# Remove desktop files
sudo rm -f /usr/local/share/applications/boost-dashboard.desktop
sudo rm -f /etc/xdg/autostart/boost-tray.desktop

# Restore BIOS defaults
sudo restore
```

### Restore BIOS defaults before uninstalling
```bash
sudo restore
```
This reverts CPU governor, EPP, turbo, RAPL limits, GPU power limits, I/O
scheduler, THP, and fan curve to their boot-time state.
