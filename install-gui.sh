#!/usr/bin/env bash
# GUI Installer for Boost Power Manager
# You can double-click this script from your File Manager.

# Move to the script's directory
cd "$(dirname "$0")" || exit 1

# If already root (e.g., ran via sudo in terminal), just run the installer
if [ "$EUID" -eq 0 ]; then
    ./install.sh
    exit $?
fi

# Try to use pkexec for a graphical password prompt
if command -v pkexec >/dev/null 2>&1; then
    # We use pkexec to run the standard install.sh
    pkexec env DISPLAY="$DISPLAY" WAYLAND_DISPLAY="$WAYLAND_DISPLAY" XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" bash ./install.sh
    
    # Check if it succeeded
    if [ $? -eq 0 ]; then
        if command -v zenity >/dev/null 2>&1; then
            zenity --info --title="Boost Power Manager" --text="Instalare completă cu succes!\n\nAplicația rulează acum automat, iar iconița este disponibilă în System Tray."
        fi
    else
        if command -v zenity >/dev/null 2>&1; then
            zenity --error --title="Boost Power Manager" --text="A apărut o eroare la instalare sau ai anulat cererea de parolă."
        fi
    fi
else
    # Fallback if no pkexec (open a terminal to ask for sudo)
    if command -v gnome-terminal >/dev/null 2>&1; then
        gnome-terminal -- bash -c "sudo ./install.sh; echo ''; read -p 'Apasă Enter pentru a închide...'"
    elif command -v konsole >/dev/null 2>&1; then
        konsole -e bash -c "sudo ./install.sh; echo ''; read -p 'Apasă Enter pentru a închide...'"
    elif command -v xfce4-terminal >/dev/null 2>&1; then
        xfce4-terminal -e "bash -c \"sudo ./install.sh; echo ''; read -p 'Apasă Enter pentru a închide...'\""
    else
        echo "Please run ./install.sh from a terminal using sudo."
    fi
fi
