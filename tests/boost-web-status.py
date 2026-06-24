#!/usr/bin/env python3
"""Regression checks for dashboard status helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path


root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("boost_web", root / "lib" / "boost-web.py")
assert spec and spec.loader
boost_web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boost_web)

summer = boost_web.mode_thresholds("summer")
assert summer["tempHot"] == 74
assert summer["boostTempLimit"] == 70

adjusted = boost_web.apply_ambient_adjustment(summer, {"detected": True, "temp": 30})
assert adjusted["tempHot"] == 72
assert adjusted["boostTempLimit"] == 67

reason = boost_web.decision_reason(
    "summer",
    "balanced",
    79,
    95,
    summer,
    {"reason": "Suggestions are available."},
)
assert "Not suggesting Boost because CPU is 79 C" in reason
assert "summer Boost limit is 70 C" in reason

print("boost web status helpers ok")
