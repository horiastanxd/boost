#!/usr/bin/env bash
# Verify Auto mode preset thresholds without touching /etc/boost-auto.conf.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output="$(BOOST_AUTO_NO_CONFIG=1 bash "$ROOT_DIR/bin/auto" modes)"
summer_output="$(BOOST_AUTO_NO_CONFIG=1 AUTO_MODE=summer AMBIENT_TEMP_C=30 bash "$ROOT_DIR/bin/auto" config)"

require_line() {
    local pattern="$1"
    if ! grep -Fq "$pattern" <<<"$output"; then
        echo "Missing preset line: $pattern" >&2
        echo "$output" >&2
        exit 1
    fi
}

require_line "calm      hot=80  critical=85  boost-below=80"
require_line "summer    hot=74  critical=82  boost-below=70"
require_line "friendly  hot=78  critical=85  boost-below=78"
require_line "active    hot=76  critical=85  boost-below=76"
require_line "quiet     hot=78  critical=85  boost-below=78"
require_line "off       hot=78  critical=85  boost-below=78"

if ! grep -Fq "Warm computer: 72" <<<"$summer_output"; then
    echo "Summer ambient adjustment did not lower TEMP_HOT" >&2
    echo "$summer_output" >&2
    exit 1
fi

if ! grep -Fq "Boost allowed below: 67" <<<"$summer_output"; then
    echo "Summer ambient adjustment did not lower BOOST_TEMP_LIMIT" >&2
    echo "$summer_output" >&2
    exit 1
fi

echo "auto mode presets ok"
