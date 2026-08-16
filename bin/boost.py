#!/usr/bin/env python3
"""Cross-platform Boost entry point.

On Linux the native `boost`, `powersave`, `silent` and `restore` shell commands
remain the real interface — this script simply forwards to them through the
platform backend, so it is available but never required.

On Windows it *is* the interface: PyInstaller freezes this file into boost.exe
(see packaging/windows/build.ps1), which is why it depends on nothing outside
the standard library.

    boost.py boost | powersave | silent | restore
    boost.py status          one-shot text summary
    boost.py web             start the local dashboard
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

def _app_root() -> Path:
    """Directory that owns lib/.

    Frozen by PyInstaller, ``__file__`` points into a temporary extraction
    directory, so the payload shipped beside boost.exe is what we want.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


LIB_DIR = _app_root() / "lib"
sys.path.insert(0, str(LIB_DIR))

import boost_paths  # noqa: E402
import platform_backend  # noqa: E402

VERSION = "1.10.0"
PROFILES = ("boost", "powersave", "silent", "restore")


def print_status(backend: platform_backend.PlatformBackend) -> None:
    temp = backend.get_cpu_temp()
    load = backend.get_cpu_load()
    gpu = backend.get_gpu_stats()

    def show(value: str) -> str:
        return value if value else "n/a"

    print(f"Platform      : {backend.name}")
    print(f"Power profile : {backend.power_profile()}")
    print(f"CPU temp      : {temp}°C" if temp else "CPU temp      : n/a")
    print(f"CPU load      : {load}%")
    print(f"GPU power     : {show(gpu.get('power', ''))} W of {show(gpu.get('limit', ''))} W")
    print(f"GPU temp      : {show(gpu.get('temp', ''))}°C")
    print(f"Config        : {boost_paths.CONF_FILE}")
    print(f"State         : {boost_paths.STATE_DIR}")

    unsupported = [
        label for label, ok in (
            ("fan control", backend.supports_fan_control),
            ("auto daemon", backend.supports_auto_daemon),
            ("RAPL power limits", backend.supports_rapl),
            ("energy performance preference", backend.supports_epp),
        ) if not ok
    ]
    if unsupported:
        print(f"Not available : {', '.join(unsupported)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="boost", description="Boost power manager (cross-platform entry point)"
    )
    parser.add_argument("--version", action="version", version=f"boost {VERSION}")
    parser.add_argument(
        "command",
        choices=[*PROFILES, "status", "web"],
        help="profile to apply, or 'status' / 'web'",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web dashboard bind address")
    parser.add_argument("--port", type=int, default=8765, help="web dashboard port")
    args = parser.parse_args(argv)

    backend = platform_backend.get_backend()

    if args.command == "status":
        print_status(backend)
        return 0

    if args.command == "web":
        import importlib.util

        web_file = LIB_DIR / "boost-web.py"
        if not web_file.is_file():
            print(f"boost: dashboard server missing at {web_file}", file=sys.stderr)
            return 1
        spec = importlib.util.spec_from_file_location("boost_web", web_file)
        if spec is None or spec.loader is None:
            print("boost: could not load boost-web.py", file=sys.stderr)
            return 1
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        server = module.ThreadingHTTPServer((args.host, args.port), module.Handler)
        print(f"Boost web dashboard: http://{args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    boost_paths.ensure_state_dir()
    result = backend.apply_profile(args.command)
    print(result.get("message", ""))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
