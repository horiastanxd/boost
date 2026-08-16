Boost for Windows — beta
========================

Boost is a Linux power manager. This is a partial Windows port: it does the
things Windows exposes through supported interfaces, and refuses the rest
rather than pretending.

What works
----------
  Power profiles   Performance / Balanced / Eco, applied with powercfg, plus
                   the processor boost mode (turbo).
  Restore          Puts back the power plan that was active before you first
                   ran Boost.
  GPU power limit  NVIDIA only, via nvidia-smi, clamped to the range the
                   driver reports.
  Dashboard        The same local web dashboard as on Linux, at
                   http://127.0.0.1:8765
  Telemetry        CPU load, GPU watts / temperature / utilisation, and the
                   ACPI thermal zone temperature where the firmware exposes it.

What does not, and why
----------------------
  Fan control      Needs a signed kernel driver to reach the embedded
                   controller. A userspace tool that half-owns the fans is
                   more dangerous than one that leaves them to the firmware.
  RAPL power limits
  Energy Performance Preference
                   Both are Linux kernel interfaces with no Windows analogue
                   that is safe to drive from userspace.
  Auto daemon      Temperature/load automation, game detection and the fan
                   engine are Linux-only for now.
  Tray applet      GTK3 and Ayatana are Linux desktop components.

Usage
-----
  boost.exe status        show what this machine reports
  boost.exe boost         maximum performance
  boost.exe powersave     balanced
  boost.exe silent        power saver, turbo off
  boost.exe restore       back to the plan you had before
  boost.exe web           start the dashboard, then open 127.0.0.1:8765

Changing the power plan and the GPU limit both need administrator rights.
Run boost.exe from an elevated prompt, or right-click -> Run as administrator.
Without elevation Boost tells you so instead of silently doing nothing.

Files
-----
  Configuration and state live in  %ProgramData%\Boost
  The dashboard sources are in     lib\webui\  — edit them and restart.

This is a portable folder: there is nothing to install and nothing to
uninstall beyond deleting it and %ProgramData%\Boost.
