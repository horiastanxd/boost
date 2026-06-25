# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.3.0+  | Yes       |
| < 1.3.0 | No        |

## Reporting a Vulnerability

Report security issues via **GitHub private vulnerability reporting**: go to the repository Security tab and click "Report a vulnerability".

Alternatively, email **team@theonerecords.com**.

Please include a description of the issue, steps to reproduce, and the potential impact. We will respond within **72 hours** and keep you informed as we work on a fix. We ask that you do not disclose the issue publicly until a fix has been released.

## Scope

**In scope:**

- Code execution via the web dashboard (the web server runs as root when started by systemd)
- Privilege escalation via `install.sh` or any installed script
- Unsafe writes to system paths (`/etc`, `/usr`, `/sys`, `/proc`, etc.)

**Out of scope:**

- Denial of service against the web dashboard (it binds to `127.0.0.1` only and is not reachable from the network)
- Attacks that require physical access to the machine

## Security Notes

- The web dashboard binds exclusively to `127.0.0.1:8765` and is not exposed to the local network or the internet.
- POST requests to `/api/action` require a matching `Origin` header, preventing cross-origin requests from other local apps or browser tabs.
