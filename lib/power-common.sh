#!/usr/bin/env bash
# /usr/local/lib/power-common.sh - shared helpers for boost/powersave

ORIGINALS_FILE="/var/lib/power-profile/originals.env"

check_root() {
    if [[ $EUID -ne 0 ]]; then
        exec sudo "$0" "$@"
    fi
}

save_originals() {
    [[ -f "$ORIGINALS_FILE" ]] && return
    mkdir -p "$(dirname "$ORIGINALS_FILE")"
    cat > "$ORIGINALS_FILE" << EOF
ORIG_GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)
ORIG_EPP=$(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)
ORIG_TURBO=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)
ORIG_PL1=$(cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null)
ORIG_PL2=$(cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_1_power_limit_uw 2>/dev/null)
ORIG_GPU_LIMIT=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits 2>/dev/null | awk '{printf "%d", $1}')
ORIG_THP=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | grep -oP '\[\K[^\]]+')
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
        echo "  [WARN] PL${constraint}: ${limit_uw}uW > hw max ${max_uw}uW, clamped"
        limit_uw="$max_uw"
    fi
    safe_write "$limit_uw" "${base}/constraint_${constraint}_power_limit_uw"
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

show_status() {
    echo ""
    echo "--- Status ---"
    sensors 2>/dev/null | grep -E "Package id|Core 0:|Core 28:|fan2" || true
    local gpu_info
    gpu_info=$(nvidia-smi --query-gpu=temperature.gpu,power.draw,power.limit --format=csv,noheader 2>/dev/null)
    [[ -n "$gpu_info" ]] && echo "GPU:      $gpu_info"
    echo "Governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
    echo "EPP:      $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"
    echo "Turbo:    $([ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)" = "0" ] && echo ON || echo OFF)"
    local pl1 pl2
    pl1=$(( $(cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))
    pl2=$(( $(cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_1_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))
    echo "PL1/PL2:  ${pl1}W / ${pl2}W"
    echo "THP:      $(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | grep -oP '\[\K[^\]]+')"
}

# Verify a /sys value was actually written (readback check)
verify_write() {
    local expected="$1" path="$2" label="$3"
    local actual
    actual=$(cat "$path" 2>/dev/null)
    # For scheduler files the value appears as "[none]" or "none [mq-deadline]"
    if echo "$actual" | grep -q "\[${expected}\]\|^${expected}$"; then
        return 0
    fi
    echo "  [WARN] verify failed $label: expected=$expected actual=$actual" >&2
    return 1
}
