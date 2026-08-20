#!/usr/bin/env bash
# /usr/local/lib/power-common.sh - shared helpers for boost/powersave/silent/restore
# Version: 1.7.0

# shellcheck disable=SC2034
# Sourced by profile scripts for --version.
readonly VERSION="1.10.0"
ORIGINALS_FILE="/var/lib/power-profile/originals.env"
FAN_BACKUP="/var/lib/power-profile/fan-curve-backup.env"
SILENT_PENDING_FILE="/var/lib/power-profile/silent-pending"
# Fan controller hwmon — discovered at source time, not hardcoded
HWMON=""
for _hwmon_dir in /sys/class/hwmon/hwmon*; do
    [[ -f "${_hwmon_dir}/pwm1_auto_point1_pwm" ]] && HWMON="$_hwmon_dir" && break
done
unset _hwmon_dir
# Directory this library lives in, so helpers can find their python siblings
# both when installed (/usr/local/lib) and when run from a repo checkout.
BOOST_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PPD_BIN="$(command -v powerprofilesctl 2>/dev/null || true)"
TUNED_BIN="$(command -v tuned-adm 2>/dev/null || true)"
AUTO_CONF_FILE="/etc/boost-auto.conf"
AUTO_SERVICE="boost-auto.service"
STATS_FILE="/var/lib/power-profile/stats.csv"
BATTERY_SUPPLY=""
_CACHED_BATTERY_CAPACITY=""

# Colors for CLI styling
if [[ -t 1 ]]; then
    readonly C_RESET="\e[0m"
    readonly C_BOLD="\e[1m"
    readonly C_CYAN="\e[36m"
    readonly C_GREEN="\e[32m"
    readonly C_YELLOW="\e[33m"
    readonly C_RED="\e[31m"
    readonly C_GRAY="\e[90m"
else
    readonly C_RESET=""
    readonly C_BOLD=""
    readonly C_CYAN=""
    readonly C_GREEN=""
    readonly C_YELLOW=""
    readonly C_RED=""
    readonly C_GRAY=""
fi

check_root() {
    if [[ $EUID -ne 0 ]]; then
        exec sudo "$0" "$@"
    fi
}

# ── Safe config reader (no eval, no source) ──────────────────────────

# Reads a KEY=VALUE config file safely, exporting only valid variable names.
# Usage: read_safe_config /path/to/config.conf
# Sets global variables, never executes code.
read_safe_config() {
    local config_file="$1" line key val
    [[ -f "$config_file" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Strip leading/trailing whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        # Skip comments and empty lines
        [[ -z "$line" || "$line" == \#* ]] && continue
        # Must contain = and key must be a valid shell identifier
        [[ "$line" != *=* ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        # Validate key: alphanumeric + underscore only
        [[ "$key" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || continue
        # Never clobber shell-critical variables from a config file
        case "$key" in
            PATH|IFS|HOME|SHELL|ENV|BASH_ENV|PS1|PS4|EUID|UID|PPID|BASH*|LD_*) continue ;;
        esac
        # Strip quotes from value if present
        val="${val%\"}"
        val="${val#\"}"
        val="${val%\'}"
        val="${val#\'}"
        printf -v "$key" "%s" "$val"
    done < "$config_file"
}

# ── Canonical mode presets (shared with lib/boost-daemon.py, lib/boost-web.py) ─

# Loads TEMP_HOT/BOOST_TEMP_LIMIT/LOAD_HIGH/LOAD_HIGH_DURATION/LOAD_IDLE/
# LOAD_IDLE_DURATION/PROMPT_COOLDOWN for one named mode out of the shared
# presets.json, so preset numbers only ever live in one place (see
# CHANGELOG v1.2.0 for the drift bug this replaces).
# Usage: load_mode_preset <mode> <presets_json_path>
load_mode_preset() {
    local mode="$1" presets_file="$2" json_out key val
    [[ -f "$presets_file" ]] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    json_out="$(python3 - "$mode" "$presets_file" <<'PYEOF'
import json, sys
mode, path = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        preset = json.load(f)[mode]
except Exception:
    sys.exit(1)
mapping = {
    "tempHot": "TEMP_HOT", "boostTempLimit": "BOOST_TEMP_LIMIT",
    "loadHigh": "LOAD_HIGH", "loadHighDuration": "LOAD_HIGH_DURATION",
    "loadIdle": "LOAD_IDLE", "loadIdleDuration": "LOAD_IDLE_DURATION",
    "promptCooldown": "PROMPT_COOLDOWN",
}
for json_key, bash_key in mapping.items():
    if json_key in preset:
        print(f"{bash_key}={preset[json_key]}")
PYEOF
)" || return 1
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^[A-Z_]+$ ]] || continue
        printf -v "$key" '%s' "$val"
    done <<< "$json_out"
    return 0
}

# ── Battery helpers ──────────────────────────────────────────────────

# Discover the battery power supply directory (cached after first call)
find_battery_supply() {
    [[ -n "$BATTERY_SUPPLY" ]] && echo "$BATTERY_SUPPLY" && return 0
    local supply
    for supply in /sys/class/power_supply/*/; do
        local type
        type=$(cat "${supply}type" 2>/dev/null || true)
        [[ "$type" == "Battery" ]] || continue
        BATTERY_SUPPLY="$supply"
        echo "$supply"
        return 0
    done
    return 1
}

# Returns battery capacity percentage (0-100), or empty string if no battery
get_battery_pct() {
    local supply
    supply=$(find_battery_supply) || { echo ""; return 1; }
    local cap
    cap=$(cat "${supply}capacity" 2>/dev/null || echo "")
    [[ -z "$cap" ]] && { echo ""; return 1; }
    echo "$cap"
}

# Returns 1 if AC is online, 0 if on battery, empty if unknown
get_ac_online() {
    local supply
    for supply in /sys/class/power_supply/*/; do
        local type
        type=$(cat "${supply}type" 2>/dev/null || true)
        [[ "$type" == "Mains" ]] || continue
        cat "${supply}online" 2>/dev/null || echo ""
        return 0
    done
    echo ""
}

# Returns "charging", "discharging", "full", "unknown"
get_battery_status() {
    local supply
    supply=$(find_battery_supply) || { echo "unknown"; return 1; }
    cat "${supply}status" 2>/dev/null || echo "unknown"
}

# Returns battery voltage in microvolts, or empty
get_battery_voltage() {
    local supply
    supply=$(find_battery_supply) || { echo ""; return 1; }
    local uv
    uv=$(cat "${supply}voltage_now" 2>/dev/null || echo "")
    echo "$uv"
}

set_auto_config_value() {
    local key="$1" value="$2" escaped
    touch "$AUTO_CONF_FILE"
    if grep -qE "^[[:space:]]*${key}=" "$AUTO_CONF_FILE"; then
        # Escape sed replacement metacharacters so values with \ & | never corrupt the file
        escaped=$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')
        sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${escaped}|" "$AUTO_CONF_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$AUTO_CONF_FILE"
    fi
}

disable_auto_for_manual_profile() {
    # Stops the daemon's automatic *profile switching* only. The daemon
    # process itself keeps running with AUTO_MODE=off (a real, supported
    # mode — boost-daemon.py's tick loop still runs the fan curve engine,
    # sensors and live snapshot in that state, it just skips the "pick a
    # profile for you" logic). Previously this fully stopped
    # boost-auto.service, which also killed the fan engine that ticks it —
    # fans would fail back to raw board control seconds after any manual
    # profile click (Performance/Balanced/Eco/Default), independent of
    # whatever fan preset was configured.
    local profile_name="$1"
    [[ "${AUTO_HELPER_INTERNAL:-0}" == "1" ]] && return 0

    set_auto_config_value AUTO_MODE off
    if systemctl is-active --quiet "$AUTO_SERVICE" 2>/dev/null; then
        echo "[AUTO] Disabled automatic profile switching because you chose ${profile_name} manually."
        echo "[AUTO] Fan curves keep running. Run 'auto mode dynamic|gaming|creator|quiet' to re-enable automatic switching."
        logger -t power-profile "auto mode set to off after manual ${profile_name}"
    fi
}

# Write an EPP value to every cpufreq policy; returns 0 if at least one write succeeded
write_epp_all() {
    local epp="$1" epp_file wrote=1
    for epp_file in /sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do
        [[ -f "$epp_file" ]] || continue
        echo "$epp" > "$epp_file" 2>/dev/null && wrote=0
    done
    return "$wrote"
}

# Set GNOME power mode via power-profiles-daemon if available
# Falls back to manual governor/EPP when ppd is absent
set_cpu_profile() {
    local ppd_profile="$1"   # performance | balanced | power-saver
    local gov="$2"            # fallback governor
    local epp="$3"            # fallback EPP
    local epp_override="${4:-}"  # optional: EPP forced after ppd applies its profile

    if [[ -n "$PPD_BIN" ]] && systemctl is-active --quiet power-profiles-daemon 2>/dev/null; then
        if "$PPD_BIN" set "$ppd_profile" 2>/dev/null; then
            echo "[CPU]  power-profiles-daemon -> $ppd_profile (GNOME synced)"
            if [[ -n "$epp_override" ]] && write_epp_all "$epp_override"; then
                echo "[CPU]  EPP override -> $epp_override (thermal-aware)"
            fi
            return
        fi
        echo "[CPU]  power-profiles-daemon rejected $ppd_profile, falling back to manual governor/EPP"
    fi

    if [[ -n "$TUNED_BIN" ]] && systemctl is-active --quiet tuned 2>/dev/null; then
        local tuned_profile="balanced"
        case "$ppd_profile" in
            performance) tuned_profile="throughput-performance" ;;
            power-saver) tuned_profile="powersave" ;;
            throughput-performance|latency-performance|accelerator-performance|desktop|balanced|balanced-battery|powersave)
                tuned_profile="$ppd_profile"
                ;;
            *) tuned_profile="balanced" ;;
        esac
        "$TUNED_BIN" profile "$tuned_profile" 2>/dev/null && \
            echo "[CPU]  tuned -> $tuned_profile"
    fi

    local wrote_gov=0 wrote_epp=0 gov_file epp_file
    for gov_file in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        [[ -f "$gov_file" ]] || continue
        if echo "$gov" > "$gov_file" 2>/dev/null; then
            wrote_gov=1
        fi
    done
    for epp_file in /sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do
        [[ -f "$epp_file" ]] || continue
        if echo "$epp" > "$epp_file" 2>/dev/null; then
            wrote_epp=1
        fi
    done
    if [[ "$wrote_epp" == "1" ]]; then
        echo "[CPU]  governor=$gov, EPP=$epp (manual)"
    elif [[ "$wrote_gov" == "1" ]]; then
        echo "[CPU]  governor=$gov (manual; EPP unsupported)"
    else
        echo "[CPU]  no writable cpufreq governor found"
    fi
}

find_hwmon_by_name() {
    local pattern="$1" hwmon name
    for hwmon in /sys/class/hwmon/hwmon*; do
        [[ -r "${hwmon}/name" ]] || continue
        name=$(cat "${hwmon}/name" 2>/dev/null)
        [[ "$name" =~ $pattern ]] && printf '%s\n' "$hwmon" && return 0
    done
    return 1
}

find_amd_gpu_hwmon() {
    local card hwmon name
    for card in /sys/class/drm/card*/; do
        [[ -d "${card}device/hwmon" ]] || continue
        for hwmon in "${card}device/hwmon/hwmon"*/; do
            [[ -r "${hwmon}name" ]] || continue
            name=$(cat "${hwmon}name" 2>/dev/null)
            [[ "$name" == "amdgpu" ]] && printf '%s\n' "$hwmon" && return 0
        done
    done
    return 1
}

_CACHED_CPU_TEMP_FILE=""
get_cpu_temp_c() {
    local raw
    if [[ -n "$_CACHED_CPU_TEMP_FILE" && -r "$_CACHED_CPU_TEMP_FILE" ]]; then
        raw=$(cat "$_CACHED_CPU_TEMP_FILE" 2>/dev/null || echo 0)
        if [[ "$raw" -gt 0 ]]; then
            echo $(( raw / 1000 ))
            return 0
        fi
    fi

    local hwmon label_file input_file label
    hwmon=$(find_hwmon_by_name '^(coretemp|k10temp|zenpower|amd_energy|macsmc_hwmon)$' 2>/dev/null || true)
    [[ -z "$hwmon" ]] && return 1

    if [[ "$(cat "${hwmon}/name" 2>/dev/null)" == "macsmc_hwmon" ]]; then
        local max_raw=0 temp_input
        for temp_input in "$hwmon"/temp*_input; do
            [[ -r "$temp_input" ]] || continue
            raw=$(cat "$temp_input" 2>/dev/null || echo 0)
            [[ "$raw" -gt "$max_raw" ]] && max_raw="$raw" && _CACHED_CPU_TEMP_FILE="$temp_input"
        done
        [[ "$max_raw" -gt 0 ]] || return 1
        echo $(( max_raw / 1000 ))
        return 0
    fi

    for label_file in "$hwmon"/temp*_label; do
        [[ -r "$label_file" ]] || continue
        label=$(cat "$label_file" 2>/dev/null)
        case "$label" in
            "Package id 0"|"Tctl"|"Tdie"|"Tccd1"|"Tccd2"|"WiFi/BT Module Temp"|"NAND Flash Temperature"|"Composite"|"Battery Hotspot")
                input_file="${label_file%_label}_input"
                raw=$(cat "$input_file" 2>/dev/null || echo 0)
                if [[ "$raw" -gt 0 ]]; then
                    _CACHED_CPU_TEMP_FILE="$input_file"
                    echo $(( raw / 1000 ))
                    return 0
                fi
                ;;
        esac
    done

    input_file="$hwmon/temp1_input"
    raw=$(cat "$input_file" 2>/dev/null || echo 0)
    [[ "$raw" -gt 0 ]] || return 1
    _CACHED_CPU_TEMP_FILE="$input_file"
    echo $(( raw / 1000 ))
}


read_cpu_totals() {
    local cpu user nice system idle iowait irq softirq steal guest guest_nice
    read -r cpu user nice system idle iowait irq softirq steal guest guest_nice < /proc/stat
    local total=$(( user + nice + system + idle + iowait + irq + softirq + steal + guest + guest_nice ))
    local idle_total=$(( idle + iowait ))
    printf '%s %s\n' "$total" "$idle_total"
}

get_cpu_load_percent() {
    local total_a idle_a total_b idle_b delta_total delta_idle
    read -r total_a idle_a < <(read_cpu_totals)
    sleep 0.2
    read -r total_b idle_b < <(read_cpu_totals)
    delta_total=$(( total_b - total_a ))
    delta_idle=$(( idle_b - idle_a ))
    [[ "$delta_total" -le 0 ]] && echo 0 && return
    echo $(( (delta_total - delta_idle) * 100 / delta_total ))
}

get_rapl_limit_w() {
    local constraint="$1"
    local base="/sys/class/powercap/intel-rapl/intel-rapl:0"
    [[ -d "$base" ]] || { echo 0; return; }
    local value
    value=$(cat "${base}/constraint_${constraint}_power_limit_uw" 2>/dev/null || echo 0)
    echo $(( value / 1000000 ))
}

get_power_profile() {
    if command -v powerprofilesctl >/dev/null 2>&1; then
        powerprofilesctl get 2>/dev/null && return
    fi
    if command -v tuned-adm >/dev/null 2>&1; then
        tuned-adm active 2>/dev/null | sed 's/^Current active profile: //' && return
    fi
    echo unknown
}

get_governor() {
    cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor 2>/dev/null ||
        cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null ||
        echo unknown
}

get_epp() {
    cat /sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference 2>/dev/null ||
        cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null ||
        echo unsupported
}

get_turbo_state() {
    if [[ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
        [[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)" = "0" ]] && echo ON || echo OFF
    elif [[ -f /sys/devices/system/cpu/cpufreq/boost ]]; then
        [[ "$(cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null)" = "1" ]] && echo ON || echo OFF
    elif [[ -f /sys/devices/system/cpu/amd_pstate/boost ]]; then
        [[ "$(cat /sys/devices/system/cpu/amd_pstate/boost 2>/dev/null)" = "1" ]] && echo ON || echo OFF
    else
        echo "unsupported"
    fi
}

get_gpu_csv() {
    # NVIDIA first
    if command -v nvidia-smi >/dev/null 2>&1; then
        local _out
        _out=$(nvidia-smi --query-gpu=temperature.gpu,power.draw,power.limit \
            --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ' || true)
        [[ -n "$_out" ]] && echo "$_out" && return 0
    fi
    # AMD GPU via amdgpu sysfs (power values in µW)
    local _amd_hwmon
    _amd_hwmon=$(find_amd_gpu_hwmon 2>/dev/null || true)
    if [[ -n "$_amd_hwmon" ]]; then
        local _temp _power_uw _cap_uw
        _temp=$(( $(cat "${_amd_hwmon}temp1_input" 2>/dev/null || echo 0) / 1000 ))
        _power_uw=$(cat "${_amd_hwmon}power1_average" 2>/dev/null || echo 0)
        _cap_uw=$(cat "${_amd_hwmon}power1_cap" 2>/dev/null || echo 0)
        printf '%d,%d.00,%d.00\n' "$_temp" "$(( _power_uw / 1000000 ))" "$(( _cap_uw / 1000000 ))"
        return 0
    fi
    true  # No GPU found; callers check for empty output
}

# Header shared with lib/boost-daemon.py's STATS_HEADER; keep both in sync.
STATS_HEADER="epoch,iso,profile,cpu_load,cpu_temp,gpu_temp,gpu_power,gpu_limit,pl1,pl2,governor,epp,turbo,battery_pct,battery_status,nvme_temp,ram_temp,vrm_temp,board_temp"

ensure_stats_file() {
    mkdir -p "$(dirname "$STATS_FILE")"
    if [[ ! -f "$STATS_FILE" ]]; then
        echo "$STATS_HEADER" > "$STATS_FILE"
        return
    fi
    # Widen an older header in place so existing history stays readable.
    if [[ "$(head -n 1 "$STATS_FILE")" != "$STATS_HEADER" ]]; then
        local tmp="${STATS_FILE}.tmp"
        { echo "$STATS_HEADER"; tail -n +2 "$STATS_FILE"; } > "$tmp" && mv "$tmp" "$STATS_FILE"
    fi
}

# Hottest reading per component category as "nvme,ram,vrm,board" using the
# shared sensor layer (lib/sensors.py). Empty fields when unavailable.
get_component_temps_csv() {
    local script
    for script in /usr/local/lib/sensors.py "${BOOST_LIB_DIR}/sensors.py"; do
        [[ -r "$script" ]] || continue
        command -v python3 >/dev/null 2>&1 || break
        python3 - "$script" <<'PYEOF' 2>/dev/null && return 0
import importlib.util, sys
spec = importlib.util.spec_from_file_location("boost_sensors", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
values = module.get_all()
print(",".join(
    "" if module.hottest(category, values) is None else str(module.hottest(category, values))
    for category in ("nvme", "ram", "vrm", "board")
))
PYEOF
    done
    echo ",,,"
}

record_power_sample() {
    local cpu_load="${1:-}" gpu_csv gpu_temp gpu_power gpu_limit bat_pct bat_status components
    [[ -n "$cpu_load" ]] || cpu_load=$(get_cpu_load_percent)
    gpu_csv=$(get_gpu_csv)
    IFS=',' read -r gpu_temp gpu_power gpu_limit <<< "$gpu_csv"
    bat_pct=$(get_battery_pct 2>/dev/null || echo "")
    bat_status=$(get_battery_status 2>/dev/null || echo "Unknown")
    components=$(get_component_temps_csv)
    ensure_stats_file
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "$(date +%s)" \
        "$(date -Is)" \
        "$(get_power_profile)" \
        "${cpu_load:-0}" \
        "$(get_cpu_temp_c 2>/dev/null || echo 0)" \
        "${gpu_temp:-0}" \
        "${gpu_power:-0}" \
        "${gpu_limit:-0}" \
        "$(get_rapl_limit_w 0)" \
        "$(get_rapl_limit_w 1)" \
        "$(get_governor)" \
        "$(get_epp)" \
        "$(get_turbo_state)" \
        "${bat_pct}" \
        "${bat_status}" \
        "${components}" >> "$STATS_FILE"
}

save_originals() {
    [[ -f "$ORIGINALS_FILE" ]] && return
    mkdir -p "$(dirname "$ORIGINALS_FILE")"
    local ppd_profile=""
    if [[ -n "$PPD_BIN" ]]; then
        ppd_profile=$("$PPD_BIN" get 2>/dev/null) || true
    fi
    local _rapl_base="/sys/class/powercap/intel-rapl/intel-rapl:0"
    local _amd_gpu_hwmon
    _amd_gpu_hwmon=$(find_amd_gpu_hwmon 2>/dev/null || true)
    # Detect turbo state across Intel and AMD platforms (mirrors power-save-originals)
    local _orig_turbo="" _orig_turbo_type="none"
    if [[ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
        _orig_turbo=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)
        _orig_turbo_type="intel"
    elif [[ -f /sys/devices/system/cpu/cpufreq/boost ]]; then
        _orig_turbo=$(cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null)
        _orig_turbo_type="cpufreq"
    elif [[ -f /sys/devices/system/cpu/amd_pstate/boost ]]; then
        _orig_turbo=$(cat /sys/devices/system/cpu/amd_pstate/boost 2>/dev/null)
        _orig_turbo_type="amd_pstate"
    elif [[ -f /sys/devices/system/cpu/cpufreq/policy0/boost ]]; then
        _orig_turbo=$(cat /sys/devices/system/cpu/cpufreq/policy0/boost 2>/dev/null)
        _orig_turbo_type="cpufreq_policy"
    fi
    cat > "$ORIGINALS_FILE" << EOF
ORIG_GOV=$(get_governor)
ORIG_EPP=$(get_epp)
ORIG_PPD_PROFILE=${ppd_profile}
ORIG_TURBO=${_orig_turbo}
ORIG_TURBO_TYPE=${_orig_turbo_type}
ORIG_PL1=$([[ -d "$_rapl_base" ]] && cat "${_rapl_base}/constraint_0_power_limit_uw" 2>/dev/null || echo "")
ORIG_PL2=$([[ -d "$_rapl_base" ]] && cat "${_rapl_base}/constraint_1_power_limit_uw" 2>/dev/null || echo "")
ORIG_GPU_LIMIT=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits 2>/dev/null | awk '{printf "%d", $1}')
ORIG_AMD_GPU_LIMIT=$([[ -n "$_amd_gpu_hwmon" ]] && echo $(( $(cat "${_amd_gpu_hwmon}power1_cap" 2>/dev/null || echo 0) / 1000000 )) || echo "0")
ORIG_THP=$(grep -oP '\[\K[^\]]+' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)
EOF
    logger -t power-profile "Originals saved"
}

safe_write() {
    local val="$1" path="$2"
    if [[ ! -f "$path" ]]; then
        echo "  [WARN] not found: $path" >&2; return 1
    fi
    if ! echo "$val" > "$path" 2>/dev/null; then
        echo "  [WARN] failed: $val -> $path" >&2; return 1
    fi
}

set_rapl() {
    local constraint="$1" limit_uw="$2"
    local base="/sys/class/powercap/intel-rapl/intel-rapl:0"
    [[ -d "$base" ]] || return 0  # No Intel RAPL on this system (AMD CPU) — skip silently
    local max_uw
    max_uw=$(cat "${base}/constraint_${constraint}_max_power_uw" 2>/dev/null || echo 0)
    if [[ "$max_uw" -gt 0 && "$limit_uw" -gt "$max_uw" ]]; then
        limit_uw="$max_uw"
    fi
    safe_write "$limit_uw" "${base}/constraint_${constraint}_power_limit_uw" 2>/dev/null || true
}

# ── Silent interlock (dumb-proof profile switching) ──────────────────

# Silent caps power *and* asks for the quietest fan curve. Applying it while
# the machine is already hot or under sustained load is how people end up
# throttling, so `silent` queues itself instead and lib/boost-daemon.py
# releases the request as soon as the machine cools down.
# Prints the reason and returns 0 when Silent must wait; returns 1 when clear.
silent_interlock_reason() {
    local temp load limit busy
    if [[ -f "$AUTO_CONF_FILE" ]]; then
        read_safe_config "$AUTO_CONF_FILE"
    fi
    limit="${BOOST_TEMP_LIMIT:-78}"
    busy="${LOAD_HIGH:-75}"
    [[ "$limit" =~ ^[0-9]+$ ]] || limit=78
    [[ "$busy" =~ ^[0-9]+$ ]] || busy=75
    temp=$(get_cpu_temp_c 2>/dev/null || echo 0)
    load=$(get_cpu_load_percent 2>/dev/null || echo 0)
    if [[ "$temp" =~ ^[0-9]+$ ]] && (( temp >= limit )); then
        echo "the CPU is at ${temp} C (limit ${limit} C)"
        return 0
    fi
    if [[ "$load" =~ ^[0-9]+$ ]] && (( load >= busy )); then
        echo "the CPU is ${load}% busy (busy threshold ${busy}%)"
        return 0
    fi
    return 1
}

queue_silent_pending() {
    local reason="$1"
    mkdir -p "$(dirname "$SILENT_PENDING_FILE")"
    printf '%s %s\n' "$(date +%s)" "$reason" > "$SILENT_PENDING_FILE"
    logger -t power-profile "silent queued: $reason"
}

clear_silent_pending() {
    rm -f "$SILENT_PENDING_FILE" 2>/dev/null || true
}

# ── GPU power limit in watts ─────────────────────────────────────────

# Prints "min max" in watts as reported by the driver, or nothing when the
# GPU has no adjustable limit. Values always come from the driver (LACT's
# approach) so the UI can never offer a watt figure the card would reject.
get_gpu_pl_range_w() {
    local limits min_l max_l amd_hwmon
    if command -v nvidia-smi >/dev/null 2>&1; then
        limits=$(nvidia-smi --query-gpu=power.min_limit,power.max_limit \
            --format=csv,noheader,nounits -i 0 2>/dev/null | head -1)
        if [[ -n "$limits" ]]; then
            IFS=',' read -r min_l max_l <<< "$limits"
            printf '%d %d\n' "$(echo "$min_l" | awk '{print int($1)}')" "$(echo "$max_l" | awk '{print int($1)}')"
            return 0
        fi
    fi
    amd_hwmon=$(find_amd_gpu_hwmon 2>/dev/null || true)
    if [[ -n "$amd_hwmon" ]]; then
        printf '%d %d\n' \
            "$(( $(cat "${amd_hwmon}power1_cap_min" 2>/dev/null || echo 0) / 1000000 ))" \
            "$(( $(cat "${amd_hwmon}power1_cap_max" 2>/dev/null || echo 0) / 1000000 ))"
        return 0
    fi
    return 1
}

get_gpu_pl_w() {
    local amd_hwmon value
    if command -v nvidia-smi >/dev/null 2>&1; then
        value=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits -i 0 2>/dev/null | awk '{printf "%d", $1}')
        [[ -n "$value" ]] && echo "$value" && return 0
    fi
    amd_hwmon=$(find_amd_gpu_hwmon 2>/dev/null || true)
    if [[ -n "$amd_hwmon" ]]; then
        echo $(( $(cat "${amd_hwmon}power1_cap" 2>/dev/null || echo 0) / 1000000 ))
        return 0
    fi
    return 1
}

get_gpu_temp_c() {
    local csv temp
    csv=$(get_gpu_csv)
    IFS=',' read -r temp _ _ <<< "$csv"
    [[ "$temp" =~ ^[0-9]+$ ]] && echo "$temp" || echo 0
}

# set_gpu_pl_w <watts> [--force]
# Clamps to the driver range and refuses to *raise* the limit while the GPU is
# already at 85 C or hotter; lowering it is always allowed.
set_gpu_pl_w() {
    local want="$1" force="${2:-}" range min_l max_l current gpu_temp amd_hwmon
    [[ "$want" =~ ^[0-9]+$ ]] || { echo "  [WARN] GPU limit must be a whole number of watts" >&2; return 1; }
    range=$(get_gpu_pl_range_w) || { echo "  [WARN] no adjustable GPU power limit on this system" >&2; return 1; }
    read -r min_l max_l <<< "$range"
    (( max_l <= 0 )) && { echo "  [WARN] driver reports no GPU power limit range" >&2; return 1; }
    (( want < min_l )) && want="$min_l"
    (( want > max_l )) && want="$max_l"
    current=$(get_gpu_pl_w 2>/dev/null || echo 0)
    gpu_temp=$(get_gpu_temp_c)
    if [[ "$force" != "--force" ]] && (( want > current )) && (( gpu_temp >= 85 )); then
        echo "[GPU]  refusing to raise the limit to ${want}W: GPU is ${gpu_temp} C" >&2
        return 2
    fi
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi -pm 1 -i 0 >/dev/null 2>&1 || true
        if nvidia-smi --power-limit="$want" -i 0 >/dev/null 2>&1; then
            echo "[GPU]  NVIDIA limit=${want}W (requested, range ${min_l}-${max_l}W)"
            return 0
        fi
        echo "  [WARN] nvidia-smi rejected the ${want}W limit" >&2
        return 1
    fi
    amd_hwmon=$(find_amd_gpu_hwmon 2>/dev/null || true)
    if [[ -n "$amd_hwmon" ]]; then
        if echo $(( want * 1000000 )) > "${amd_hwmon}power1_cap" 2>/dev/null; then
            echo "[GPU]  AMD limit=${want}W (requested, range ${min_l}-${max_l}W)"
            return 0
        fi
        echo "  [WARN] AMD GPU power limit write failed (check amdgpu driver)" >&2
        return 1
    fi
    return 1
}

# Explicit per-profile watt limit from /etc/boost-auto.conf, empty when unset.
gpu_pl_for_mode() {
    local mode="$1" value=""
    case "$mode" in
        boost)     value="${GPU_PL_BOOST_W:-}" ;;
        powersave) value="${GPU_PL_POWERSAVE_W:-}" ;;
        silent)    value="${GPU_PL_SILENT_W:-}" ;;
    esac
    [[ "$value" =~ ^[0-9]+$ ]] && (( value > 0 )) && echo "$value"
}

apply_hardware_limits() {
    local mode="$1" # boost, powersave, silent, restore

    if [[ -f "$ORIGINALS_FILE" ]]; then
        # Parse, don't source: a malformed value must never abort a profile switch
        read_safe_config "$ORIGINALS_FILE"
    fi
    if [[ -f "$AUTO_CONF_FILE" ]]; then
        read_safe_config "$AUTO_CONF_FILE"
    fi

    # Intel RAPL dynamic scaling (Intel CPUs only; AMD CPUs skip gracefully)
    local rapl_base="/sys/class/powercap/intel-rapl/intel-rapl:0"
    local pl1="${ORIG_PL1:-}" pl2="${ORIG_PL2:-}"
    if [[ -d "$rapl_base" && -n "$pl1" && -n "$pl2" && "$pl1" -gt 0 ]]; then
        local t_pl1 t_pl2
        # Thermal-aware boost: sustained (PL1) and burst (PL2) power scale from
        # BOOST_PL1_PCT / BOOST_PL2_PCT in /etc/boost-auto.conf. Capping PL2
        # trades a few percent of all-core throughput for a large temp drop.
        local b_pl1_pct="${BOOST_PL1_PCT:-100}" b_pl2_pct="${BOOST_PL2_PCT:-80}"
        [[ "$b_pl1_pct" =~ ^[0-9]+$ ]] || b_pl1_pct=100
        [[ "$b_pl2_pct" =~ ^[0-9]+$ ]] || b_pl2_pct=80
        (( b_pl1_pct < 40 )) && b_pl1_pct=40; (( b_pl1_pct > 100 )) && b_pl1_pct=100
        (( b_pl2_pct < 40 )) && b_pl2_pct=40; (( b_pl2_pct > 100 )) && b_pl2_pct=100
        case "$mode" in
            boost)
                t_pl1=$(( pl1 * b_pl1_pct / 100 )); t_pl2=$(( pl2 * b_pl2_pct / 100 ))
                ;;
            restore)
                t_pl1=$pl1; t_pl2=$pl2
                ;;
            powersave)
                t_pl1=$(( pl1 * 60 / 100 )); t_pl2=$(( pl2 * 60 / 100 ))
                ;;
            silent)
                t_pl1=$(( pl1 * 40 / 100 )); t_pl2=$(( pl2 * 40 / 100 ))
                ;;
        esac
        # Safety floor
        (( t_pl1 < 10000000 )) && t_pl1=10000000
        (( t_pl2 < 15000000 )) && t_pl2=15000000
        # PL2 (burst) must never drop below PL1 (sustained)
        (( t_pl2 < t_pl1 )) && t_pl2=$t_pl1

        set_rapl 0 "$t_pl1"
        set_rapl 1 "$t_pl2"
        echo "[CPU]  RAPL PL1=$((t_pl1 / 1000000))W, PL2=$((t_pl2 / 1000000))W (scaled for $mode)"
    fi

    # Explicit per-profile GPU watt limit wins over the percentage scaling
    # below: if the user picked a number in the dashboard or via
    # `auto gpu-limit`, that is what gets applied on every profile switch,
    # boot and resume.
    local explicit_gpu_w
    explicit_gpu_w=$(gpu_pl_for_mode "$mode")
    if [[ -n "$explicit_gpu_w" ]]; then
        # A refused write (hot GPU) must not fall through to the percentage
        # scaling below, which would raise the limit the guard just blocked.
        set_gpu_pl_w "$explicit_gpu_w" || true
        return 0
    fi

    # NVIDIA GPU dynamic scaling
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi -pm 1 -i 0 >/dev/null 2>&1 || true
        local def_gpu="${ORIG_GPU_LIMIT:-}"
        local limits min_l max_l t_gpu
        limits=$(nvidia-smi --query-gpu=power.min_limit,power.max_limit --format=csv,noheader,nounits -i 0 2>/dev/null | head -1)
        if [[ -n "$limits" ]]; then
            IFS=',' read -r min_l max_l <<< "$limits"
            min_l=$(echo "$min_l" | awk '{print int($1)}')
            max_l=$(echo "$max_l" | awk '{print int($1)}')

            [[ -z "$def_gpu" || "$def_gpu" -eq 0 ]] && def_gpu=$(( min_l + (max_l - min_l) / 2 ))

            case "$mode" in
                boost) t_gpu=$max_l ;;
                restore) t_gpu=$def_gpu ;;
                powersave) t_gpu=$(( min_l + (def_gpu - min_l) / 2 )) ;;
                silent) t_gpu=$min_l ;;
            esac

            (( t_gpu < min_l )) && t_gpu=$min_l
            (( t_gpu > max_l )) && t_gpu=$max_l

            nvidia-smi --power-limit="${t_gpu}" -i 0 >/dev/null 2>&1 || true
            echo "[GPU]  NVIDIA limit=${t_gpu}W (scaled for $mode)"
        fi
    # AMD GPU dynamic scaling (amdgpu sysfs; power values in µW)
    else
        local amd_hwmon
        amd_hwmon=$(find_amd_gpu_hwmon 2>/dev/null || true)
        if [[ -n "$amd_hwmon" ]]; then
            local cap_uw cap_max_uw cap_min_uw def_amd t_cap_uw
            cap_uw=$(cat "${amd_hwmon}power1_cap" 2>/dev/null || echo 0)
            cap_max_uw=$(cat "${amd_hwmon}power1_cap_max" 2>/dev/null || echo 0)
            cap_min_uw=$(cat "${amd_hwmon}power1_cap_min" 2>/dev/null || echo 0)
            def_amd="${ORIG_AMD_GPU_LIMIT:-0}"
            # def_amd stored in W; convert to µW for comparison
            local def_amd_uw=$(( def_amd * 1000000 ))
            [[ "$def_amd_uw" -le 0 ]] && def_amd_uw="$cap_uw"

            case "$mode" in
                boost)    t_cap_uw="$cap_max_uw" ;;
                restore)  t_cap_uw="$def_amd_uw" ;;
                powersave) t_cap_uw=$(( cap_min_uw + (def_amd_uw - cap_min_uw) / 2 )) ;;
                silent)   t_cap_uw="$cap_min_uw" ;;
            esac

            (( cap_min_uw > 0 && t_cap_uw < cap_min_uw )) && t_cap_uw="$cap_min_uw"
            (( cap_max_uw > 0 && t_cap_uw > cap_max_uw )) && t_cap_uw="$cap_max_uw"

            if echo "$t_cap_uw" > "${amd_hwmon}power1_cap" 2>/dev/null; then
                echo "[GPU]  AMD limit=$(( t_cap_uw / 1000000 ))W (scaled for $mode)"
            else
                echo "  [WARN] AMD GPU power limit write failed (check amdgpu driver)" >&2
            fi
        fi
    fi
}

set_turbo() {
    local state="$1"  # on | off
    if [[ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
        local val=0
        [[ "$state" == "off" ]] && val=1
        safe_write "$val" /sys/devices/system/cpu/intel_pstate/no_turbo
    elif [[ -f /sys/devices/system/cpu/cpufreq/boost ]]; then
        local val=1
        [[ "$state" == "off" ]] && val=0
        safe_write "$val" /sys/devices/system/cpu/cpufreq/boost
    elif [[ -f /sys/devices/system/cpu/amd_pstate/boost ]]; then
        local val=1
        [[ "$state" == "off" ]] && val=0
        safe_write "$val" /sys/devices/system/cpu/amd_pstate/boost
    elif [[ -f /sys/devices/system/cpu/cpufreq/policy0/boost ]]; then
        local val=1 policy_boost
        [[ "$state" == "off" ]] && val=0
        for policy_boost in /sys/devices/system/cpu/cpufreq/policy*/boost; do
            [[ -f "$policy_boost" ]] || continue
            safe_write "$val" "$policy_boost" 2>/dev/null || true
        done
    else
        echo "[CPU]  turbo unsupported on this platform"
        return 0
    fi
    echo "[CPU]  turbo=${state^^}"
}

set_io_schedulers() {
    local dev blkdev rota
    for dev in /sys/block/nvme*/queue/scheduler /sys/block/sd*/queue/scheduler; do
        [[ -f "$dev" ]] || continue
        blkdev=$(echo "$dev" | cut -d/ -f4)
        rota=$(cat "/sys/block/${blkdev}/queue/rotational" 2>/dev/null)
        if [[ "$rota" == "0" ]]; then
            safe_write "none" "$dev" && echo "  [I/O] ${blkdev} -> none (SSD/NVMe)"
        else
            safe_write "mq-deadline" "$dev" && echo "  [I/O] ${blkdev} -> mq-deadline (HDD)"
        fi
    done
}

save_fan_curve() {
    [[ ! -d "$HWMON" ]] && return 1
    {
        for i in 1 2 3 4 5; do
            echo "ORIG_PWM_PT${i}_PWM=$(cat "${HWMON}/pwm1_auto_point${i}_pwm" 2>/dev/null)"
            echo "ORIG_PWM_PT${i}_TEMP=$(cat "${HWMON}/pwm1_auto_point${i}_temp" 2>/dev/null)"
        done
        echo "ORIG_PWM1_ENABLE=$(cat "${HWMON}/pwm1_enable" 2>/dev/null)"
        echo "ORIG_PWM1_FLOOR=$(cat "${HWMON}/pwm1_floor" 2>/dev/null)"
    } > "$FAN_BACKUP"
    logger -t power-profile "Fan curve backed up"
}

restore_fan_curve() {
    [[ ! -f "$FAN_BACKUP" || ! -d "$HWMON" ]] && return 0
    read_safe_config "$FAN_BACKUP"
    for i in 1 2 3 4 5; do
        local pwm_var="ORIG_PWM_PT${i}_PWM" temp_var="ORIG_PWM_PT${i}_TEMP"
        safe_write "${!pwm_var}" "${HWMON}/pwm1_auto_point${i}_pwm" 2>/dev/null || true
        safe_write "${!temp_var}" "${HWMON}/pwm1_auto_point${i}_temp" 2>/dev/null || true
    done
    safe_write "${ORIG_PWM1_FLOOR:-1}" "${HWMON}/pwm1_floor" 2>/dev/null || true
    echo "[FAN]  Smart Fan IV curve restored"
}

# ── WiFi / Bluetooth / USB power saving ──────────────────────────────

# Set WiFi power save (on|off). Uses iw if available, else sysfs.
set_wifi_powersave() {
    local state="$1"  # on | off
    if command -v iw >/dev/null 2>&1; then
        local iface
        for iface in /sys/class/net/wlan*/; do
            [[ -d "$iface" ]] || continue
            iface=$(basename "$iface")
            if iw dev "$iface" set power_save "$state" 2>/dev/null; then
                echo "  [NET] $iface power_save=$state"
            fi
        done
    fi
    # Also set device power control via sysfs
    local dev
    for dev in /sys/class/net/wlan*/device/power/control; do
        [[ -f "$dev" ]] || continue
        local val="on"
        [[ "$state" == "on" ]] && val="auto"
        safe_write "$val" "$dev" 2>/dev/null && \
            echo "  [NET] $(echo "$dev" | cut -d/ -f5) power/control=$val"
    done
}

# Set Bluetooth power control (auto|on)
set_bt_power() {
    local state="$1"  # powersave | performance
    local val="on"
    [[ "$state" == "powersave" ]] && val="auto"
    local dev
    for dev in /sys/class/bluetooth/*/device/power/control; do
        [[ -f "$dev" ]] || continue
        safe_write "$val" "$dev" 2>/dev/null && \
            echo "  [BT]  $(echo "$dev" | cut -d/ -f5) power/control=$val"
    done
}

# Set USB autosuspend (auto|on)
# HID input devices (keyboards, mice) are always kept at "on" — autosuspend
# on these can leave them unresponsive until a wake event, especially over
# hubs or wireless dongles.
set_usb_autosuspend() {
    local state="$1"  # powersave | performance
    local val="on"
    [[ "$state" == "powersave" ]] && val="auto"
    local count=0
    local dev devdir devname iface is_hid
    for dev in /sys/bus/usb/devices/*/power/control; do
        [[ -f "$dev" ]] || continue
        devdir="${dev%/power/control}"
        devname="${devdir##*/}"
        is_hid=0
        for iface in "${devdir}/${devname}":*; do
            [[ -e "$iface" ]] || continue
            [[ "$(readlink -f "${iface}/driver" 2>/dev/null)" == */usbhid ]] && { is_hid=1; break; }
        done
        if [[ "$is_hid" == "1" ]]; then
            safe_write "on" "$dev" 2>/dev/null
            continue
        fi
        safe_write "$val" "$dev" 2>/dev/null && ((count++))
    done
    [[ $count -gt 0 ]] && echo "  [USB] $count devices power/control=$val (keyboards/mice excluded)"
}

# Set PCI Express ASPM (via sysfs)
set_pcie_aspm() {
    local state="$1"  # powersave | performance
    local policy="performance"
    [[ "$state" == "powersave" ]] && policy="powersave"
    if [[ -f /sys/module/pcie_aspm/parameters/policy ]]; then
        safe_write "$policy" /sys/module/pcie_aspm/parameters/policy 2>/dev/null && \
            echo "  [PCI] ASPM=$policy"
    fi
}

# Apply all peripheral power saving for a given mode
apply_peripheral_power() {
    local mode="$1"  # boost | powersave | silent | restore
    case "$mode" in
        boost|restore)
            set_wifi_powersave off
            set_bt_power performance
            set_usb_autosuspend performance
            set_pcie_aspm performance
            ;;
        powersave)
            set_wifi_powersave on
            set_bt_power powersave
            set_usb_autosuspend powersave
            set_pcie_aspm powersave
            ;;
        silent)
            set_wifi_powersave on
            set_bt_power powersave
            set_usb_autosuspend powersave
            set_pcie_aspm powersave
            ;;
    esac
}

reset_process_priorities() {
    local user="$1"
    [[ -z "$user" ]] && return 0
    local count=0
    while IFS= read -r pid; do
        if renice -n 0 -p "$pid" > /dev/null 2>&1; then
            ((count++))
        fi
    done < <(ps -u "$user" -o pid= 2>/dev/null)
    [[ $count -gt 0 ]] && echo "[PROC] $count processes -> nice 0"
}

# Known process names for automatic Game Mode. Mirrors GAME_PROCESSES in
# lib/boost-daemon.py (used there to decide *when* to auto-boost); keep
# both lists in sync.
GAME_PROCESS_NAMES=(wine-preloader wine64-preloader proton steam cs2 dota2 hl2_linux)

# Renice the detected foreground game process(es) to -5 and set realtime
# I/O priority during Boost, mirroring Feral GameMode's per-PID approach
# instead of relying only on the blanket nice-0 reset_process_priorities()
# above does for every other profile switch.
boost_game_priority() {
    local name pid count=0
    command -v ionice >/dev/null 2>&1
    local have_ionice=$?
    for name in "${GAME_PROCESS_NAMES[@]}"; do
        while IFS= read -r pid; do
            [[ -n "$pid" ]] || continue
            renice -n -5 -p "$pid" >/dev/null 2>&1 && ((count++))
            [[ $have_ionice -eq 0 ]] && ionice -c 1 -p "$pid" >/dev/null 2>&1
        done < <(pgrep -x "$name" 2>/dev/null)
    done
    [[ $count -gt 0 ]] && echo "[PROC] $count game process(es) -> nice -5, ionice realtime"
}

draw_bar() {
    local val="$1" max="${2:-100}" color="${3:-$C_GREEN}"
    local pct=$(( val * 100 / max ))
    (( pct > 100 )) && pct=100
    (( pct < 0 )) && pct=0
    local width=10
    local filled=$(( pct * width / 100 ))
    local empty=$(( width - filled ))
    local bar=""
    local i
    for ((i=0; i<filled; i++)); do bar+="█"; done
    for ((i=0; i<empty; i++)); do bar+="░"; done
    printf "${color}%s${C_RESET}" "$bar"
}

show_status() {
    echo -e "\n${C_CYAN}${C_BOLD}┌────────────────── Boost Status ──────────────────┐${C_RESET}"
    local gpu_csv gpu_temp gpu_power gpu_limit pl1 pl2 cpu_load cpu_temp ppd gov epp turbo thp
    gpu_csv=$(get_gpu_csv)
    IFS=',' read -r gpu_temp gpu_power gpu_limit <<< "$gpu_csv"
    
    cpu_load=$(get_cpu_load_percent)
    cpu_temp=$(get_cpu_temp_c 2>/dev/null || echo 0)
    ppd=$(get_power_profile)
    gov=$(get_governor)
    epp=$(get_epp)
    turbo=$(get_turbo_state)
    pl1=$(get_rapl_limit_w 0)
    pl2=$(get_rapl_limit_w 1)
    thp=$(grep -oP '\[\K[^\]]+' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)
    
    # Load color
    local load_color=$C_GREEN
    if (( cpu_load > 80 )); then load_color=$C_RED; elif (( cpu_load > 50 )); then load_color=$C_YELLOW; fi
    
    # Temp color
    local temp_color=$C_GREEN
    if (( cpu_temp >= 80 )); then temp_color=$C_RED; elif (( cpu_temp >= 70 )); then temp_color=$C_YELLOW; fi
    
    printf "  ${C_BOLD}%-13s${C_RESET} %3d%%  [%-10b]\n" "CPU Load:" "$cpu_load" "$(draw_bar "$cpu_load" 100 "$load_color")"
    printf "  ${C_BOLD}%-13s${C_RESET} %3d°C  [%-10b]\n" "CPU Temp:" "$cpu_temp" "$(draw_bar "$cpu_temp" 100 "$temp_color")"
    
    if [[ -n "$gpu_csv" && "$gpu_temp" -gt 0 ]]; then
        local gpu_p_val=${gpu_power%%.*}
        local gpu_l_val=${gpu_limit%%.*}
        [[ -z "$gpu_p_val" ]] && gpu_p_val=0
        [[ -z "$gpu_l_val" || "$gpu_l_val" -eq 0 ]] && gpu_l_val=150
        local gpu_color=$C_GREEN
        if (( gpu_temp >= 80 )); then gpu_color=$C_RED; elif (( gpu_temp >= 70 )); then gpu_color=$C_YELLOW; fi
        printf "  ${C_BOLD}%-13s${C_RESET} %3d°C  [%-10b]  %3dW / %3dW limit\n" "GPU:" "$gpu_temp" "$(draw_bar "$gpu_temp" 100 "$gpu_color")" "$gpu_p_val" "$gpu_l_val"
    fi
    
    # PPD color
    local ppd_disp="$ppd"
    if [[ "$ppd" == "performance" || "$ppd" == "throughput-performance" || "$ppd" == "latency-performance" || "$ppd" == "accelerator-performance" ]]; then ppd_disp="${C_RED}Performance${C_RESET}"
    elif [[ "$ppd" == "balanced" ]]; then ppd_disp="${C_GREEN}Balanced${C_RESET}"
    elif [[ "$ppd" == "power-saver" || "$ppd" == "powersave" || "$ppd" == "balanced-battery" ]]; then ppd_disp="${C_CYAN}Eco Mode${C_RESET}"; fi
    
    # Turbo color
    local turbo_disp="$turbo"
    if [[ "$turbo" == "ON" ]]; then turbo_disp="${C_RED}ON (Boost enabled)${C_RESET}"
    elif [[ "$turbo" == "OFF" ]]; then turbo_disp="${C_CYAN}OFF (Disabled)${C_RESET}"; fi
    
    echo -e "  ${C_GRAY}──────────────────────────────────────────────────${C_RESET}"
    printf "  %-13s %b\n" "Profile (PPD):" "$ppd_disp"
    printf "  %-13s %s (epp: %s)\n" "Governor:" "$gov" "$epp"
    printf "  %-13s %b\n" "Turbo Boost:" "$turbo_disp"
    printf "  %-13s %sW / %sW\n" "RAPL PL1/PL2:" "$pl1" "$pl2"
    printf "  %-13s %s\n" "THP (Hugepgs):" "$thp"
    echo -e "${C_CYAN}${C_BOLD}└──────────────────────────────────────────────────┘${C_RESET}"
}

verify_write() {
    local expected="$1" path="$2"
    local actual
    actual=$(cat "$path" 2>/dev/null)
    echo "$actual" | grep -q "\[${expected}\]\|^${expected}$"
}
