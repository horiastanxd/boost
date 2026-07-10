# Dependencies

## Runtime Dependencies

### Core (required for all functionality)

| Dependency | Minimum Version | Purpose | Package Name (Ubuntu/Debian) | Package Name (Fedora/RHEL) | Package Name (Arch) |
|-----------|----------------|---------|------------------------------|----------------------------|---------------------|
| bash | 4.0 | Script runtime | `bash` | `bash` | `bash` |
| python3 | 3.8 | Daemon, web server, tray | `python3` | `python3` | `python` |
| systemd | 240 | Service management | `systemd` | `systemd` | `systemd` |
| sudo | — | Privilege escalation | `sudo` | `sudo` | `sudo` |
| coreutils | — | `cat`, `echo`, `sleep`, etc. | `coreutils` | `coreutils` | `coreutils` |
| util-linux | — | `renice`, `ionice` | `util-linux` | `util-linux` | `util-linux` |
| procps | — | `ps`, `pgrep` | `procps` | `procps` | `procps` |
| grep | — | Pattern matching | `grep` | `grep` | `grep` |
| sed | — | Config file editing | `sed` | `sed` | `sed` |

### Power Management

| Dependency | Minimum Version | Purpose | Package Name (Ubuntu/Debian) | Package Name (Fedora/RHEL) | Package Name (Arch) |
|-----------|----------------|---------|------------------------------|----------------------------|---------------------|
| power-profiles-daemon | 0.10 | GNOME power profile sync | `power-profiles-daemon` | `power-profiles-daemon` | `power-profiles-daemon` |
| linux-tools-common | — | `cpupower` (alternative) | `linux-tools-common` | `kernel-tools` | — |

### GPU Support

| Dependency | Purpose | Notes |
|-----------|---------|-------|
| `nvidia-smi` (NVIDIA driver) | NVIDIA GPU power limit + monitoring | Required for NVIDIA GPU support |
| `amdgpu` kernel driver | AMD GPU power limit + monitoring | Built into mainline kernel; no extra package needed |

### Notifications

| Dependency | Purpose | Package Name (Ubuntu/Debian) | Package Name (Fedora/RHEL) | Package Name (Arch) |
|-----------|---------|------------------------------|----------------------------|---------------------|
| `notify-send` (libnotify) | Desktop notifications | `libnotify-bin` | `libnotify` | `libnotify` |

### System Tray Applet (`boost-tray`)

| Dependency | Minimum Version | Purpose | Package Name (Ubuntu/Debian) | Package Name (Fedora/RHEL) | Package Name (Arch) |
|-----------|----------------|---------|------------------------------|----------------------------|---------------------|
| python3-gi (PyGObject) | 3.30 | GTK3 Python bindings | `python3-gi` | `python3-gobject` | `python-gobject` |
| gir1.2-gtk-3.0 | 3.22 | GTK3 toolkit | `gir1.2-gtk-3.0` | `gtk3` | `gtk3` |
| gir1.2-ayatanaappindicator3-0.1 | 0.5 | System tray indicator | `gir1.2-ayatanaappindicator3-0.1` | `libayatana-appindicator-gtk3` | `libayatana-appindicator` |
| gir1.2-notify-0.7 | 0.7 | Desktop notifications (GTK) | `gir1.2-notify-0.7` | `libnotify` | `libnotify` |

**Note:** On GNOME 42+ with the default extension, tray icons may not appear
unless you install an extension like
[AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/appindicator-and-kstatusnotifieritem-support/).

### Web Dashboard

| Dependency | Purpose | Notes |
|-----------|---------|-------|
| python3 (stdlib) | HTTP server | Uses only stdlib (`http.server`, `json`, `csv`, `threading`) |
| A web browser | Viewing the dashboard | Any modern browser (Chrome, Firefox, Edge, Brave, etc.) |

No additional pip packages are required for the web dashboard.

## Optional Dependencies

| Dependency | Purpose | Package Name (Ubuntu/Debian) |
|-----------|---------|------------------------------|
| `lm-sensors` | Additional sensor data | `lm-sensors` |
| `xdg-utils` | Opening URLs from CLI | `xdg-utils` |
| `loginctl` (systemd) | User session detection | `systemd` (included) |
| `journalctl` (systemd) | Viewing daemon logs | `systemd` (included) |

## Build / Development Dependencies

| Dependency | Purpose | Package Name |
|-----------|---------|-------------|
| `ruff` | Python linting | `pip install ruff` |
| `pytest` | Python testing | `pip install pytest` |
| shellcheck | Shell script linting | `shellcheck` |
| python3 | Compile check | `python3` |

## Filesystem Paths Used

| Path | Purpose | Created By |
|------|---------|-----------|
| `/usr/local/bin/boost` | Profile command | `install.sh` |
| `/usr/local/bin/powersave` | Profile command | `install.sh` |
| `/usr/local/bin/silent` | Profile command | `install.sh` |
| `/usr/local/bin/restore` | Profile command | `install.sh` |
| `/usr/local/bin/summer` | Summer mode shortcut | `install.sh` |
| `/usr/local/bin/auto` | Auto mode CLI | `install.sh` |
| `/usr/local/bin/power-report` | Report generator | `install.sh` |
| `/usr/local/bin/boost-web` | Web server launcher | `install.sh` |
| `/usr/local/bin/ac-event` | AC event handler | `install.sh` |
| `/usr/local/bin/power-save-originals` | Boot state capture | `install.sh` |
| `/usr/local/bin/boost-tray` | Tray applet | `install.sh` |
| `/usr/local/lib/power-common.sh` | Shared shell library | `install.sh` |
| `/usr/local/lib/boost-web.py` | Web server | `install.sh` |
| `/usr/local/lib/boost-daemon.py` | Auto daemon | `install.sh` |
| `/etc/boost-auto.conf` | Configuration | `install.sh` |
| `/var/lib/power-profile/` | State directory | Runtime |
| `/etc/systemd/system/boost-auto.service` | Systemd service | `install.sh` |
| `/etc/systemd/system/boost-web.service` | Systemd service | `install.sh` |
| `/etc/systemd/system/power-save-originals.service` | Systemd service | `install.sh` |
| `/etc/udev/rules.d/99-boost-power.rules` | udev rule | `install.sh` |
| `/usr/share/bash-completion/completions/auto` | Bash completion | `install.sh` |

## Quick Install Commands

### Ubuntu / Debian
```bash
sudo apt update
sudo apt install -y \
  bash python3 systemd sudo coreutils util-linux procps grep sed \
  power-profiles-daemon libnotify-bin \
  python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7
```

### Fedora / RHEL
```bash
sudo dnf install -y \
  bash python3 systemd sudo coreutils util-linux procps grep sed \
  power-profiles-daemon libnotify \
  python3-gobject gtk3 libayatana-appindicator-gtk3 libnotify
```

### Arch Linux
```bash
sudo pacman -S --needed \
  bash python systemd sudo coreutils util-linux procps grep sed \
  power-profiles-daemon libnotify \
  python-gobject gtk3 libayatana-appindicator libnotify
```
