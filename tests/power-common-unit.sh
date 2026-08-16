#!/usr/bin/env bash
# Unit tests for lib/power-common.sh helpers that don't need root/hardware.
# Follows the same hand-rolled assertion style as auto-mode-presets.sh
# (no bats/shunit2 dependency). Each test runs in its own subshell so a
# `source` or variable leak in one test can't contaminate the next; the
# subshell's exit code (not a shared variable) reports pass/fail.

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOTAL_FAILURES=0

# ── read_safe_config: rejects shell-critical keys, parses valid ones ────

test_read_safe_config_rejects_injection() (
    set -u
    local_failures=0
    fail() { echo "FAIL: $1" >&2; local_failures=$((local_failures + 1)); }
    assert_eq() { [[ "$1" == "$2" ]] || fail "$3: expected '$2', got '$1'"; }

    source "$ROOT_DIR/lib/power-common.sh"
    conf="$(mktemp)"
    trap 'rm -f "$conf"' EXIT
    cat > "$conf" <<'EOF'
PATH=/tmp/evil
IFS=x
LD_PRELOAD=/tmp/evil.so
BASH_ENV=/tmp/evil.sh
# a comment
TEMP_HOT=82
AUTO_MODE=gaming
EOF
    PATH_BEFORE="$PATH"
    read_safe_config "$conf"
    assert_eq "$PATH" "$PATH_BEFORE" "PATH must not be clobbered by config"
    assert_eq "${TEMP_HOT:-}" "82" "TEMP_HOT should parse normally"
    assert_eq "${AUTO_MODE:-}" "gaming" "AUTO_MODE should parse normally"
    assert_eq "${LD_PRELOAD:-}" "" "LD_PRELOAD must never be set from config"
    exit "$local_failures"
)

# ── load_mode_preset: matches the canonical presets.json ────────────────

test_load_mode_preset_matches_json() (
    set -u
    local_failures=0
    fail() { echo "FAIL: $1" >&2; local_failures=$((local_failures + 1)); }
    assert_eq() { [[ "$1" == "$2" ]] || fail "$3: expected '$2', got '$1'"; }

    source "$ROOT_DIR/lib/power-common.sh"
    load_mode_preset gaming "$ROOT_DIR/presets.json" || fail "load_mode_preset(gaming) returned non-zero"
    assert_eq "${TEMP_HOT:-}" "80" "gaming TEMP_HOT"
    assert_eq "${BOOST_TEMP_LIMIT:-}" "80" "gaming BOOST_TEMP_LIMIT"
    assert_eq "${LOAD_HIGH:-}" "50" "gaming LOAD_HIGH"
    assert_eq "${LOAD_HIGH_DURATION:-}" "30" "gaming LOAD_HIGH_DURATION"
    assert_eq "${LOAD_IDLE:-}" "10" "gaming LOAD_IDLE"
    assert_eq "${LOAD_IDLE_DURATION:-}" "600" "gaming LOAD_IDLE_DURATION"
    assert_eq "${PROMPT_COOLDOWN:-}" "900" "gaming PROMPT_COOLDOWN"
    exit "$local_failures"
)

test_load_mode_preset_unknown_mode_fails() (
    set -u
    source "$ROOT_DIR/lib/power-common.sh"
    if load_mode_preset bogus-mode "$ROOT_DIR/presets.json" 2>/dev/null; then
        echo "FAIL: load_mode_preset(bogus-mode) should return non-zero" >&2
        exit 1
    fi
    exit 0
)

test_load_mode_preset_missing_file_fails() (
    set -u
    source "$ROOT_DIR/lib/power-common.sh"
    if load_mode_preset dynamic "/nonexistent/presets.json" 2>/dev/null; then
        echo "FAIL: load_mode_preset with a missing file should return non-zero" >&2
        exit 1
    fi
    exit 0
)

# ── boost_game_priority: no-op and no error when no game is running ────

test_boost_game_priority_noop_when_no_game() (
    set -u
    source "$ROOT_DIR/lib/power-common.sh"
    GAME_PROCESS_NAMES=(definitely-not-a-real-process-xyz)
    out="$(boost_game_priority)"
    if [[ -n "$out" ]]; then
        echo "FAIL: boost_game_priority should print nothing when no game process matches (got: $out)" >&2
        exit 1
    fi
    exit 0
)

run_test() {
    local name="$1"
    if "$name"; then
        echo "PASS: $name"
    else
        echo "FAIL: $name"
        TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
    fi
}

run_test test_read_safe_config_rejects_injection
run_test test_load_mode_preset_matches_json
run_test test_load_mode_preset_unknown_mode_fails
run_test test_load_mode_preset_missing_file_fails
run_test test_boost_game_priority_noop_when_no_game

if [[ $TOTAL_FAILURES -eq 0 ]]; then
    echo "power-common unit tests ok"
    exit 0
else
    echo "$TOTAL_FAILURES test(s) failed" >&2
    exit 1
fi
