#!/usr/bin/env bash
# /usr/local/lib/power-common.sh - shared helpers for boost/powersave/silent/restore
# Version: 1.1.0

# shellcheck disable=SC2034
# Sourced by profile scripts for --version.
readonly VERSION="1.2.0"
ORIGINALS_FILE="/var/lib/power-profile/originals.env"
FAN_BACKUP="/var/lib/power-profile/fan-curve-backup.env"
# Fan controller hwmon — discovered at source time, not hardcoded
HWMON=""
for _hwmon_dir in /sys/class/hwmon/hwmon*; do
    [[ -f "${_hwmon_dir}/pwm1_auto_point1_pwm" ]] && HWMON="$_hwmon_dir" && break
done
unset _hwmon_dir
PPD_BIN="$(command -v powerprofilesctl 2>/dev/null)"
AUTO_CONF_FILE="/etc/boost-auto.conf"
AUTO_SERVICE="boost-auto.service"
STATS_FILE="/var/lib/power-profile/stats.csv"

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

find_hwmon_by_name() {
    local pattern="$1" hwmon name
    for hwmon in /sys/class/hwmon/hwmon*; do
        [[ -r "${hwmon}/name" ]] || continue
        name=$(cat "${hwmon}/name" 2>/dev/null)
        [[ "$name" =~ $pattern ]] && printf '%s\n' "$hwmon" && return 0
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
    hwmon=$(find_hwmon_by_name '^(coretemp|k10temp|zenpower|amd_energy)$' 2>/dev/null || true)
    [[ -z "$hwmon" ]] && return 1

    for label_file in "$hwmon"/temp*_label; do
        [[ -r "$label_file" ]] || continue
        label=$(cat "$label_file" 2>/dev/null)
        case "$label" in
            "Package id 0"|"Tctl"|"Tdie"|"Tccd1"|"Tccd2")
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
    local constraint="$1" value
    value=$(cat "/sys/class/powercap/intel-rapl/intel-rapl:0/constraint_${constraint}_power_limit_uw" 2>/dev/null || echo 0)
    echo $(( value / 1000000 ))
}

get_power_profile() {
    powerprofilesctl get 2>/dev/null || echo unknown
}

get_governor() {
    cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown
}

get_epp() {
    cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null || echo unknown
}

get_turbo_state() {
    if [[ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
        [[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)" = "0" ]] && echo ON || echo OFF
    elif [[ -f /sys/devices/system/cpu/cpufreq/boost ]]; then
        [[ "$(cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null)" = "1" ]] && echo ON || echo OFF
    elif [[ -f /sys/devices/system/cpu/amd_pstate/boost ]]; then
        [[ "$(cat /sys/devices/system/cpu/amd_pstate/boost 2>/dev/null)" = "1" ]] && echo ON || echo OFF
    else
        echo "OFF"
    fi
}

get_gpu_csv() {
    nvidia-smi --query-gpu=temperature.gpu,power.draw,power.limit \
        --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ' || true
}

ensure_stats_file() {
    mkdir -p "$(dirname "$STATS_FILE")"
    if [[ ! -f "$STATS_FILE" ]]; then
        echo "epoch,iso,profile,cpu_load,cpu_temp,gpu_temp,gpu_power,gpu_limit,pl1,pl2,governor,epp,turbo" > "$STATS_FILE"
    fi
}

record_power_sample() {
    local cpu_load="${1:-}" gpu_csv gpu_temp gpu_power gpu_limit
    [[ -n "$cpu_load" ]] || cpu_load=$(get_cpu_load_percent)
    gpu_csv=$(get_gpu_csv)
    IFS=',' read -r gpu_temp gpu_power gpu_limit <<< "$gpu_csv"
    ensure_stats_file
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
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
        "$(get_turbo_state)" >> "$STATS_FILE"
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
        limit_uw="$max_uw"
    fi
    safe_write "$limit_uw" "${base}/constraint_${constraint}_power_limit_uw" 2>/dev/null || true
}

apply_hardware_limits() {
    local mode="$1" # boost, powersave, silent, restore
    
    if [[ -f "$ORIGINALS_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$ORIGINALS_FILE"
    fi
    
    # Intel RAPL dynamic scaling
    local pl1="${ORIG_PL1:-}" pl2="${ORIG_PL2:-}"
    if [[ -n "$pl1" && -n "$pl2" && "$pl1" -gt 0 ]]; then
        local t_pl1 t_pl2
        case "$mode" in
            boost|restore)
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
        
        set_rapl 0 "$t_pl1"
        set_rapl 1 "$t_pl2"
        echo "[CPU]  RAPL PL1=$((t_pl1 / 1000000))W, PL2=$((t_pl2 / 1000000))W (scaled for $mode)"
    fi
    
    # NVIDIA dynamic scaling
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
    if [[ "$ppd" == "performance" ]]; then ppd_disp="${C_RED}Performance (Boost)${C_RESET}"
    elif [[ "$ppd" == "balanced" ]]; then ppd_disp="${C_GREEN}Balanced (Powersave)${C_RESET}"
    elif [[ "$ppd" == "power-saver" ]]; then ppd_disp="${C_CYAN}Power Saver (Silent)${C_RESET}"; fi
    
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
