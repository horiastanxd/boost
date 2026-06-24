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

creator = boost_web.mode_thresholds("creator")
assert creator["tempHot"] == 82
assert creator["boostTempLimit"] == 82

reason = boost_web.decision_reason(
    "creator",
    "balanced",
    83,
    95,
    creator,
    {"reason": "Suggestions are available."},
)
assert "Not suggesting Boost because CPU is 83 C" in reason
assert "creator Boost limit is 82 C" in reason

print("boost web status helpers ok")
