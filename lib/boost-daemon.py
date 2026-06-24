#!/usr/bin/env python3
"""
Boost Power Daemon
Unified async daemon replacing the bash loop for maximum efficiency.
Features: O(1) thermal/load polling, Game Mode detection.
"""
import os
import sys
import time
import subprocess
import syslog
import threading
from datetime import datetime

STATE_DIR = "/var/lib/power-profile"
CONF_FILE = "/etc/boost-auto.conf"
STATS_FILE = os.path.join(STATE_DIR, "stats.csv")
SNOOZE_FILE = os.path.join(STATE_DIR, "auto-snooze-until")
SKIP_TODAY_FILE = os.path.join(STATE_DIR, "auto-skip-date")

# Known process names for automatic Game Mode
GAME_PROCESSES = ['wine-preloader', 'wine64-preloader', 'proton', 'steam', 'cs2', 'dota2', 'hl2_linux']

class BoostDaemon:
    def __init__(self):
        syslog.openlog("boost-auto", syslog.LOG_PID, syslog.LOG_USER)
        self.mode = "dynamic"
        self.poll_interval = 5
        self.stats_interval = 60
        self.temp_hot = 78
        self.temp_critical = 85
        self.boost_temp_limit = 78
        self.load_high = 75
        self.load_high_duration = 120
        self.load_idle = 8
        self.load_idle_duration = 600
        self.prompt_cooldown = 900
        self.allow_critical = "yes"
        self.quiet_start = "22:00"
        self.quiet_end = "08:00"
        self.summer_nights = "no"
        
        self.cpu_temp_path = self.find_cpu_temp_path()
        self.prev_total = 0
        self.prev_idle = 0
        
        self.high_since = 0
        self.idle_since = 0
        self.last_prompt = 0
        self.last_auto = 0
        self.last_stats = 0
        self.last_conf_mtime = -1
        self.cached_session = None
        self.cached_env = None
        self._cached_profile = None
        self._profile_cycle_count = 0
        self._snooze_cache = (0, 0, False)
        
    def log(self, msg, level=syslog.LOG_INFO):
        syslog.syslog(level, msg)

    def find_cpu_temp_path(self):
        hwmon_base = "/sys/class/hwmon"
        if not os.path.exists(hwmon_base):
            return None
        for hwmon in os.listdir(hwmon_base):
            hwmon_path = os.path.join(hwmon_base, hwmon)
            name_file = os.path.join(hwmon_path, "name")
            if os.path.exists(name_file):
                with open(name_file, 'r') as f:
                    name = f.read().strip()
                if name in ['coretemp', 'k10temp', 'zenpower', 'amd_energy']:
                    for f in os.listdir(hwmon_path):
                        if f.endswith('_label') and f.startswith('temp'):
                            try:
                                with open(os.path.join(hwmon_path, f), 'r') as lbl:
                                    label = lbl.read().strip()
                                if label in {"Package id 0", "Tctl", "Tdie", "Tccd1", "Tccd2"}:
                                    return os.path.join(hwmon_path, f.replace('_label', '_input'))
                            except Exception: pass
                    fallback = os.path.join(hwmon_path, "temp1_input")
                    if os.path.exists(fallback):
                        return fallback
        return None

    def read_config(self):
        if not os.path.exists(CONF_FILE): return
        try:
            with open(CONF_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'): continue
                    if '=' in line:
                        k, v = [x.strip() for x in line.split('=', 1)]
                        if k == "AUTO_MODE": self.mode = v
                        elif k == "ALLOW_CRITICAL_AUTO": self.allow_critical = v
                        elif k == "TEMP_CRITICAL": self.temp_critical = int(v)
                        elif k == "TEMP_HOT": self.temp_hot = int(v)
                        elif k == "BOOST_TEMP_LIMIT": self.boost_temp_limit = int(v)
                        elif k == "LOAD_HIGH": self.load_high = int(v)
                        elif k == "LOAD_IDLE": self.load_idle = int(v)
                        elif k == "QUIET_HOURS_START": self.quiet_start = v
                        elif k == "QUIET_HOURS_END": self.quiet_end = v
                        elif k == "SUMMER_SILENT_NIGHTS": self.summer_nights = v
        except Exception: pass

    def apply_preset(self):
        if self.mode == "dynamic":
            self.temp_hot, self.boost_temp_limit = 78, 78
            self.load_high, self.load_high_duration = 75, 120
            self.load_idle, self.load_idle_duration = 8, 600
            self.prompt_cooldown = 900
        elif self.mode == "gaming":
            # Baseline for gaming, allows higher temps, quicker boosting
            self.temp_hot, self.boost_temp_limit = 80, 80
            self.load_high, self.load_high_duration = 50, 30
            self.load_idle, self.load_idle_duration = 10, 600
            self.prompt_cooldown = 900
        elif self.mode == "creator":
            # For AI training / rendering: high sustained load required before boosting,
            # high temp limits, very long idle required before cooling down.
            self.temp_hot, self.boost_temp_limit = 82, 82
            self.load_high, self.load_high_duration = 85, 30
            self.load_idle, self.load_idle_duration = 15, 1200
            self.prompt_cooldown = 300
        elif self.mode == "quiet":
            # Strict limits for meetings/library
            self.temp_hot, self.boost_temp_limit = 70, 70
            self.load_high, self.load_high_duration = 90, 600
            self.load_idle, self.load_idle_duration = 5, 120
            self.prompt_cooldown = 3600

    def read_cpu_temp(self):
        if not self.cpu_temp_path: return 0
        try:
            with open(self.cpu_temp_path, 'r') as f:
                return int(f.read().strip()) // 1000
        except Exception:
            return 0

    def read_cpu_load(self):
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline().strip()
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3] + parts[4]
            total = sum(parts)
            delta_total = total - self.prev_total
            delta_idle = idle - self.prev_idle
            self.prev_total = total
            self.prev_idle = idle
            if delta_total <= 0: return 0
            return int((delta_total - delta_idle) * 100 / delta_total)
        except Exception:
            return 0

    def is_game_running(self):
        try:
            return subprocess.run(
                ['pgrep', '-f', '|'.join(GAME_PROCESSES)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            ).returncode == 0
        except Exception:
            return False

    def get_ppd_profile(self):
        self._profile_cycle_count += 1
        if self._cached_profile is None or self._profile_cycle_count >= 3:
            self._profile_cycle_count = 0
            try:
                self._cached_profile = subprocess.check_output(['powerprofilesctl', 'get'], text=True).strip()
            except Exception:
                self._cached_profile = "balanced"
        return self._cached_profile

    def read_text(self, path, default=""):
        try:
            with open(path, 'r') as f: return f.read().strip()
        except: return default

    def get_gpu_stats(self):
        try:
            out = subprocess.check_output(['nvidia-smi', '--query-gpu=temperature.gpu,power.draw,power.limit', '--format=csv,noheader,nounits'], text=True).strip()
            if out:
                parts = [x.strip() for x in out.split('\n')[0].split(',')]
                if len(parts) == 3: return parts
        except: pass
        return ["0", "0", "0"]

    def record_stats(self, load, temp, profile):
        try:
            if not getattr(self, '_state_dir_created', False):
                os.makedirs(STATE_DIR, exist_ok=True)
                self._state_dir_created = True
            
            gpu_temp, gpu_power, gpu_limit = self.get_gpu_stats()
            pl1 = str(int(self.read_text('/sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw', '0')) // 1000000)
            pl2 = str(int(self.read_text('/sys/class/powercap/intel-rapl/intel-rapl:0/constraint_1_power_limit_uw', '0')) // 1000000)
            gov = self.read_text('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor', 'unknown')
            epp = self.read_text('/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference', 'unknown')
            turbo = "ON" if self.read_text('/sys/devices/system/cpu/intel_pstate/no_turbo', '1') == '0' else "OFF"
            
            iso_time = datetime.now().astimezone().replace(microsecond=0).isoformat()
            row = f"{int(time.time())},{iso_time},{profile},{load},{temp},{gpu_temp},{gpu_power},{gpu_limit},{pl1},{pl2},{gov},{epp},{turbo}\n"
            
            if not os.path.exists(STATS_FILE):
                with open(STATS_FILE, 'w') as f:
                    f.write("epoch,iso,profile,cpu_load,cpu_temp,gpu_temp,gpu_power,gpu_limit,pl1,pl2,governor,epp,turbo\n")
            
            with open(STATS_FILE, 'a') as f:
                f.write(row)
                
            # Prevent infinite growth: if file > 250KB, keep header + last 2000 lines (~1.5 days at 1 min interval)
            if os.path.getsize(STATS_FILE) > 250 * 1024:
                with open(STATS_FILE, 'r') as f:
                    lines = f.readlines()
                if len(lines) > 2000:
                    with open(f"{STATS_FILE}.tmp", 'w') as f:
                        f.write(lines[0])
                        f.writelines(lines[-2000:])
                    os.rename(f"{STATS_FILE}.tmp", STATS_FILE)
        except Exception as e:
            self.log(f"Error recording stats: {e}")

    def run_command(self, cmd):
        subprocess.Popen(cmd, shell=True, env=dict(os.environ, AUTO_HELPER_INTERNAL="1"))
        self._cached_profile = None

    def get_user_env(self):
        try:
            out = subprocess.check_output(['loginctl', 'list-sessions', '--no-legend'], text=True)
            session = None
            for line in out.strip().split('\n'):
                if not line: continue
                parts = line.split()
                if 'active' in parts or parts[0]:
                    session = parts[0]
                    break
            if not session: return None
            
            if self.cached_session == session and self.cached_env is not None:
                return self.cached_env.copy()
            
            user = subprocess.check_output(['loginctl', 'show-session', session, '-p', 'Name', '--value'], text=True).strip()
            uid = subprocess.check_output(['id', '-u', user], text=True).strip()
            
            x11 = subprocess.check_output(['loginctl', 'show-session', session, '-p', 'Display', '--value'], text=True).strip()
            if not x11: x11 = ":0"
            
            wayland = ""
            run_dir = f"/run/user/{uid}"
            if os.path.exists(run_dir):
                for f in os.listdir(run_dir):
                    if f.startswith('wayland-') and not f.endswith('.lock'):
                        wayland = f
                        break
            
            env = {
                'XDG_RUNTIME_DIR': run_dir,
                'DBUS_SESSION_BUS_ADDRESS': f"unix:path={run_dir}/bus",
                'USER': user,
                'UID': uid
            }
            if wayland:
                env['WAYLAND_DISPLAY'] = wayland
                env['GDK_BACKEND'] = 'wayland'
            else:
                env['DISPLAY'] = x11
                env['GDK_BACKEND'] = 'x11'
                
            self.cached_session = session
            self.cached_env = env.copy()
            return env
        except Exception as e:
            self.log(f"Error getting user env: {e}")
            return None

    def send_notification(self, title, body, action_label=None, action_cmd=None, level="normal"):
        env_vars = self.get_user_env()
        if not env_vars: return
            
        user = env_vars.pop('USER')
        uid = env_vars.pop('UID')
        
        cmd_env = dict(os.environ)
        cmd_env.update(env_vars)
        
        if action_label:
            cmd = ['sudo', '-u', user]
            for k, v in env_vars.items():
                cmd.extend(['env', f"{k}={v}"])
            cmd.extend(['notify-send', '--wait', '-u', level, '-a', 'Auto power helper', 
                       '--expire-time=45000', f'--action=switch={action_label}', 
                       '--action=snooze=Later', '--action=today=Not today', title, body])
            
            def handle_notify():
                try:
                    action = subprocess.check_output(cmd, env=cmd_env, text=True).strip()
                    if action == "switch":
                        self.log(f"Action accepted: {action_cmd}")
                        self.run_command(action_cmd)
                    elif action == "snooze":
                        until = int(time.time()) + 7200
                        with open(SNOOZE_FILE, 'w') as f:
                            f.write(str(until))
                        self._snooze_cache = (time.time(), until, False)
                    elif action == "today":
                        with open(SKIP_TODAY_FILE, 'w') as f:
                            f.write(datetime.now().strftime("%Y-%m-%d"))
                        self._snooze_cache = (time.time(), 0, True)
                except Exception:
                    pass
            threading.Thread(target=handle_notify).start()
        else:
            cmd = ['sudo', '-u', user]
            for k, v in env_vars.items():
                cmd.extend(['env', f"{k}={v}"])
            cmd.extend(['notify-send', '-u', level, '-a', 'Auto power helper', title, body])
            subprocess.Popen(cmd, env=cmd_env)
        
        self.log(f"NOTIFY: {title}")

    def in_quiet_hours(self):
        if self.quiet_start == self.quiet_end: return False
        try:
            now = datetime.now()
            now_m = now.hour * 60 + now.minute
            h, m = map(int, self.quiet_start.split(':'))
            start_m = h * 60 + m
            h, m = map(int, self.quiet_end.split(':'))
            end_m = h * 60 + m
            if start_m < end_m:
                return start_m <= now_m < end_m
            else:
                return now_m >= start_m or now_m < end_m
        except Exception:
            return False

    def suggestions_paused(self):
        if self.mode in ("quiet", "off"): return True
        if self.in_quiet_hours(): return True
        
        now = time.time()
        if now - self._snooze_cache[0] < 30:
            ttl, until_epoch, skipped = self._snooze_cache
            if skipped or until_epoch > now: return True
            return False

        skipped = False
        until_epoch = 0
        try:
            if os.path.exists(SKIP_TODAY_FILE):
                with open(SKIP_TODAY_FILE, 'r') as f:
                    if f.read().strip() == datetime.now().strftime("%Y-%m-%d"):
                        skipped = True
            if os.path.exists(SNOOZE_FILE):
                with open(SNOOZE_FILE, 'r') as f:
                    until_epoch = int(f.read().strip())
                if until_epoch <= now:
                    os.remove(SNOOZE_FILE)
        except Exception:
            pass
            
        self._snooze_cache = (now, until_epoch, skipped)
        return skipped or until_epoch > now

    def loop(self):
        self.log("Boost Daemon started in Python High-Performance mode")
        while True:
            time.sleep(self.poll_interval)
            
            try:
                current_mtime = os.stat(CONF_FILE).st_mtime if os.path.exists(CONF_FILE) else 0
            except Exception:
                current_mtime = 0
                
            if current_mtime != self.last_conf_mtime or self.last_conf_mtime == -1:
                self.read_config()
                self.apply_preset()
                self.last_conf_mtime = current_mtime
            
            now = int(time.time())
            temp = self.read_cpu_temp()
            load = self.read_cpu_load()
            profile = self.get_ppd_profile()
            is_game = self.is_game_running()
            
            if now - self.last_stats >= self.stats_interval:
                self.record_stats(load, temp, profile)
                self.last_stats = now
                
            if self.mode == "off":
                continue
                
            # Summer Night Mode Auto-Silent
            if self.summer_nights == "yes" and self.in_quiet_hours() and profile != "power-saver":
                if now - self.last_auto > 3600:
                    self.log("Summer quiet hours: auto silent")
                    self.run_command("/usr/local/bin/silent --auto")
                    self.send_notification("Summer night mode", "Quiet hours are active, so Auto applied Silent mode.")
                    self.last_auto = now
                    self.last_prompt = now
                    continue
            
            # Game Mode Auto-Switching
            if is_game and profile != "performance" and temp < self.boost_temp_limit:
                if now - self.last_auto > 60:
                    self.log("Game detected. Switching to boost automatically.")
                    self.run_command("/usr/local/bin/boost")
                    self.send_notification("Game Mode Enabled", "Detected a game running. Switched to maximum performance.")
                    self.last_auto = now
                    continue
            
            # Critical Protection
            if temp >= self.temp_critical and profile == "performance" and self.allow_critical == "yes":
                if now - self.last_auto > 120:
                    self.log(f"Critical heat {temp}C. Emergency powersave.")
                    self.run_command("/usr/local/bin/powersave")
                    self.send_notification("Critical Heat Warning", f"CPU reached {temp}C. Switched to cooler mode to protect hardware.", level="critical")
                    self.last_auto = now
                    self.last_prompt = now
            
            if self.suggestions_paused():
                continue
                
            # Hot Warning
            if temp >= self.temp_hot and profile == "performance":
                if now - self.last_prompt > self.prompt_cooldown:
                    self.send_notification("The computer is getting warm", "I can switch to a cooler mode.", "Cool it down", "/usr/local/bin/powersave")
                    self.last_prompt = now
            
            # High Load Warning (Non-game)
            elif load >= self.load_high and not is_game:
                if self.high_since == 0: self.high_since = now
                elif now - self.high_since >= self.load_high_duration and profile != "performance" and temp < self.boost_temp_limit:
                    if now - self.last_prompt > self.prompt_cooldown:
                        self.send_notification("It looks like you need more power", "I can enable Boost for heavy work.", "Enable Boost", "/usr/local/bin/boost")
                        self.last_prompt = now
                        self.high_since = 0
                self.idle_since = 0
            
            # Idle Warning
            elif load <= self.load_idle and not is_game:
                if self.idle_since == 0: self.idle_since = now
                elif now - self.idle_since >= self.load_idle_duration and profile == "performance":
                    if now - self.last_prompt > self.prompt_cooldown:
                        self.send_notification("The PC looks quiet now", "I can leave Boost to reduce heat.", "Cool down", "/usr/local/bin/powersave")
                        self.last_prompt = now
                        self.idle_since = 0
                self.high_since = 0
            else:
                self.high_since = 0
                self.idle_since = 0

if __name__ == "__main__":
    daemon = BoostDaemon()
    daemon.loop()
