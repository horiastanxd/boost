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
        self.mode = "friendly"
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
                        if f.endswith('_input') and f.startswith('temp'):
                            return os.path.join(hwmon_path, f)
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
        if self.mode == "calm":
            self.temp_hot, self.boost_temp_limit = 80, 80
            self.load_high, self.load_high_duration = 85, 300
            self.load_idle, self.load_idle_duration = 5, 1200
            self.prompt_cooldown = 3600
        elif self.mode == "summer":
            self.temp_critical, self.temp_hot, self.boost_temp_limit = 82, 74, 70
            self.load_high, self.load_high_duration = 90, 360
            self.load_idle, self.load_idle_duration = 15, 180
            self.prompt_cooldown = 1800
        elif self.mode == "active":
            self.temp_hot, self.boost_temp_limit = 76, 76
            self.load_high, self.load_high_duration = 65, 45
            self.load_idle, self.load_idle_duration = 12, 240
            self.prompt_cooldown = 300
        elif self.mode == "friendly":
            self.temp_hot, self.boost_temp_limit = 78, 78
            self.load_high, self.load_high_duration = 75, 120
            self.load_idle, self.load_idle_duration = 8, 600
            self.prompt_cooldown = 900

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
            out = subprocess.check_output(['ps', '-e', '-o', 'comm='], text=True)
            for p in out.strip().split('\n'):
                if any(g in p for g in GAME_PROCESSES):
                    return True
        except Exception:
            pass
        return False

    def get_ppd_profile(self):
        try:
            return subprocess.check_output(['powerprofilesctl', 'get'], text=True).strip()
        except Exception:
            return "balanced"

    def record_stats(self, load, temp, profile):
        try:
            if not os.path.exists(STATE_DIR): os.makedirs(STATE_DIR)
            subprocess.Popen(['bash', '-c', f'source /usr/local/lib/power-common.sh >/dev/null 2>&1 && record_power_sample {load}'])
        except Exception:
            pass

    def run_command(self, cmd):
        subprocess.Popen(cmd, shell=True, env=dict(os.environ, AUTO_HELPER_INTERNAL="1"))

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
                        with open(SNOOZE_FILE, 'w') as f:
                            f.write(str(int(time.time()) + 7200))
                    elif action == "today":
                        with open(SKIP_TODAY_FILE, 'w') as f:
                            f.write(datetime.now().strftime("%Y-%m-%d"))
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
            now_m = datetime.now().hour * 60 + datetime.now().minute
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
        try:
            if os.path.exists(SKIP_TODAY_FILE):
                with open(SKIP_TODAY_FILE, 'r') as f:
                    if f.read().strip() == datetime.now().strftime("%Y-%m-%d"):
                        return True
            if os.path.exists(SNOOZE_FILE):
                with open(SNOOZE_FILE, 'r') as f:
                    until_epoch = int(f.read().strip())
                if until_epoch > time.time():
                    return True
                os.remove(SNOOZE_FILE)
        except Exception:
            pass
        return False

    def loop(self):
        self.log("Boost Daemon started in Python High-Performance mode")
        while True:
            time.sleep(self.poll_interval)
            self.read_config()
            self.apply_preset()
            
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
            if self.mode == "summer" and self.summer_nights == "yes" and self.in_quiet_hours() and profile != "power-saver":
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
