---
name: Bug report
about: Script fails or produces unexpected results
title: ''
labels: bug
assignees: ''
---

**Command that failed**
```
boost / powersave / silent / restore
```

**Error output**
```
paste output here
```

**Hardware**
- CPU: 
- GPU: 
- Distro + kernel: `uname -r`
- Driver: `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver`

**Relevant sysfs state**
```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
cat /sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw
nvidia-smi --query-gpu=power.limit --format=csv,noheader
```
