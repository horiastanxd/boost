#!/usr/bin/env bash
# Verify Auto mode preset thresholds without touching /etc/boost-auto.conf.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output="$(BOOST_AUTO_NO_CONFIG=1 bash "$ROOT_DIR/bin/auto" modes)"

require_line() {
    local pattern="$1"
    if ! grep -Eq "$pattern" <<<"$output"; then
        echo "Missing preset line: $pattern" >&2
        echo "Got output:" >&2
        echo "$output" >&2
        exit 1
    fi
}

require_line "dynamic.*hot=78.*critical=85.*boost-below=78.*busy=75%/2m.*idle=8%/10m.*cooldown=15m.*balanced everyday"
require_line "creator.*hot=82.*critical=85.*boost-below=82.*busy=85%/30s.*idle=15%/20m.*cooldown=5m.*gaming/rendering limits"
require_line "quiet.*hot=70.*critical=85.*boost-below=70.*busy=90%/10m.*idle=5%/2m.*cooldown=1h 0m.*strictly low noise/heat"
require_line "off.*hot=78.*critical=85.*boost-below=78.*busy=75%/2m.*idle=8%/10m.*cooldown=15m.*disabled"

echo "auto mode presets ok"

