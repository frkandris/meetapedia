"""The governor's decision table (tools/localgpu-governor/governor.py).

Pure function, so the rules the owner was promised — full when away, smaller
while they work, off when the machine needs its memory or battery — are pinned
without a Mac in CI.
"""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools/localgpu-governor/governor.py"
_spec = importlib.util.spec_from_file_location("localgpu_governor", _PATH)
gov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gov)


def _sig(**kw):
    base = {"idle_s": 0, "ac": True, "pressure": 1, "free_pct": 50,
            "thermal": False, "paused": False}
    return {**base, **kw}


@pytest.mark.parametrize("signals,state", [
    (_sig(idle_s=600), "full"),
    (_sig(idle_s=10), "low"),
    # On a 16 GB Mac the kernel sits at "warning" with plenty free; that alone
    # must not take the model away from the owner's working hours.
    (_sig(idle_s=10, pressure=2, free_pct=46), "low"),
    (_sig(idle_s=600, free_pct=20), "low"),
    (_sig(idle_s=600, ac=False), "off"),
    (_sig(idle_s=600, pressure=4), "off"),
    (_sig(idle_s=10, free_pct=8), "off"),
    (_sig(idle_s=600, thermal=True), "off"),
    (_sig(idle_s=600, paused=True), "off"),
])
def test_desired_state(signals, state):
    assert gov.desired_state(signals, 300)[0] == state


def test_low_runs_fewer_slots_than_production_allows():
    assert gov.SLOTS["low"] < gov.SLOTS["full"] == 2
