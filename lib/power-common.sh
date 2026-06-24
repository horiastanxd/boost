#!/usr/bin/env bash
# /usr/local/lib/power-common.sh - shared helpers for boost/powersave/silent/restore
# Version: 1.1.0

# shellcheck disable=SC2034
# Sourced by profile scripts for --version.
readonly VERSION="1.1.0"
ORIGINALS_FILE="/var/lib/power-profile/originals.env"
FAN_BACKUP="/var/lib/power-profile/fan-curve-backup.env"
HWMON="/sys/class/hwmon/hwmon5"
PPD_BIN="$(command -v powerprofilesctl 2>/dev/null)"
AUTO_CONF_FILE="/etc/boost-auto.conf"
AUTO_SERVICE="boost-auto.service"

check_root() {
    if [[ $EUID -ne 0 ]]; then
        exec sudo "$0" "$@"
    fi
}

set_auto_config_value() {
    local key="$1" value="$2"
    touch "$AUTO_CONF_FILE"
    if grep -qE "^[[:space:]]*${key}=" "$AUTO_CONF_FILE"; then
        sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${value}|" "$AUTO_CONF_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$AUTO_CONF_FILE"
    fi
}

disable_auto_for_manual_profile() {
    local profile_name="$1"
    [[ "${AUTO_HELPER_INTERNAL:-0}" == "1" ]] && return 0

    set_auto_config_value AUTO_MODE off
    if systemctl is-active --quiet "$AUTO_SERVICE" 2>/dev/null; then
        systemctl stop "$AUTO_SERVICE" 2>/dev/null || true
        echo "[AUTO] Disabled auto mode because you chose ${profile_name} manually."
        echo "[AUTO] Run 'auto start' or 'auto mode calm|friendly|active' to enable it again."
        logger -t power-profile "auto disabled after manual ${profile_name}"
    fi
}

# Set GNOME power mode via power-profiles-daemon if available
# Falls back to manual governor/EPP when ppd is absent
set_cpu_profile() {
    local ppd_profile="$1"   # performance | balanced | power-saver
    local gov="$2"            # fallback governor
    local epp="$3"            # fallback EPP

    if [[ -n "$PPD_BIN" ]] && systemctl is-active --quiet power-profiles-daemon 2>/dev/null; then
        "$PPD_BIN" set "$ppd_profile" 2>/dev/null && \
            echo "[CPU]  power-profiles-daemon -> $ppd_profile (GNOME synced)"
        return
    fi

    # ppd not available - set manually
    for gov_file in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo "$gov" > "$gov_file"
    done
    for epp_file in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do
        echo "$epp" > "$epp_file"
    done
    echo "[CPU]  governor=$gov, EPP=$epp (manual)"
}

save_originals() {
    [[ -f "$ORIGINALS_FILE" ]] && return
    mkdir -p "$(dirname "$ORIGINALS_FILE")"
    local ppd_profile=""
    if [[ -n "$PPD_BIN" ]]; then
        ppd_profile=$("$PPD_BIN" get 2>/dev/null) || true
    fi
    cat > "$ORIGINALS_FILE" << EOF
ORIG_GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)
ORIG_EPP=$(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)
ORIG_PPD_PROFILE=${ppd_profile}
ORIG_TURBO=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)
ORIG_PL1=$(cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null)
ORIG_PL2=$(cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_1_power_limit_uw 2>/dev/null)
ORIG_GPU_LIMIT=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits 2>/dev/null | awk '{printf "%d", $1}')
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
    local max_uw
    max_uw=$(cat "${base}/constraint_${constraint}_max_power_uw" 2>/dev/null || echo 0)
    if [[ "$max_uw" -gt 0 && "$limit_uw" -gt "$max_uw" ]]; then
        echo "  [WARN] PL${constraint}: clamped to hw max $(( max_uw / 1000000 ))W"
        limit_uw="$max_uw"
    fi
    safe_write "$limit_uw" "${base}/constraint_${constraint}_power_limit_uw"
}

set_turbo() {
    local state="$1"  # on | off
    local val=0
    [[ "$state" == "off" ]] && val=1
    safe_write "$val" /sys/devices/system/cpu/intel_pstate/no_turbo
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
            echo "ORIG_PWM_PT${i}_PWM=$(cat ${HWMON}/pwm1_auto_point${i}_pwm 2>/dev/null)"
            echo "ORIG_PWM_PT${i}_TEMP=$(cat ${HWMON}/pwm1_auto_point${i}_temp 2>/dev/null)"
        done
        echo "ORIG_PWM1_ENABLE=$(cat ${HWMON}/pwm1_enable 2>/dev/null)"
        echo "ORIG_PWM1_FLOOR=$(cat ${HWMON}/pwm1_floor 2>/dev/null)"
    } > "$FAN_BACKUP"
    logger -t power-profile "Fan curve backed up"
}

restore_fan_curve() {
    [[ ! -f "$FAN_BACKUP" || ! -d "$HWMON" ]] && return 0
    # shellcheck source=/dev/null
    source "$FAN_BACKUP"
    for i in 1 2 3 4 5; do
        local pwm_var="ORIG_PWM_PT${i}_PWM" temp_var="ORIG_PWM_PT${i}_TEMP"
        safe_write "${!pwm_var}" "${HWMON}/pwm1_auto_point${i}_pwm" 2>/dev/null || true
        safe_write "${!temp_var}" "${HWMON}/pwm1_auto_point${i}_temp" 2>/dev/null || true
    done
    safe_write "${ORIG_PWM1_FLOOR:-1}" "${HWMON}/pwm1_floor" 2>/dev/null || true
    echo "[FAN]  Smart Fan IV curve restored"
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

show_status() {
    echo ""
    echo "--- Status ---"
    sensors 2>/dev/null | grep -E "Package id|Core 0:|Core 28:|fan2" || true
    local gpu_info
    gpu_info=$(nvidia-smi --query-gpu=temperature.gpu,power.draw,power.limit --format=csv,noheader 2>/dev/null)
    [[ -n "$gpu_info" ]] && echo "GPU:      $gpu_info"
    if [[ -n "$PPD_BIN" ]]; then
        echo "PPD:      $($PPD_BIN get 2>/dev/null)"
    fi
    echo "Governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
    echo "EPP:      $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"
    echo "Turbo:    $([ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)" = "0" ] && echo ON || echo OFF)"
    local pl1 pl2
    pl1=$(( $(cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))
    pl2=$(( $(cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_1_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))
    echo "PL1/PL2:  ${pl1}W / ${pl2}W"
    echo "THP:      $(grep -oP '\[\K[^\]]+' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)"
}

verify_write() {
    local expected="$1" path="$2"
    local actual
    actual=$(cat "$path" 2>/dev/null)
    echo "$actual" | grep -q "\[${expected}\]\|^${expected}$"
}
