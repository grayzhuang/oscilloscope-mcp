"""Profile assertion tests for the shared ``capability.acquisition``
block — guards that every registered model's profile declares the enums
and ranges the acquisition helper + tools depend on.
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp.instruments import MODEL_REGISTRY, _load_profile


@pytest.mark.parametrize("model", sorted(MODEL_REGISTRY))
def test_profile_declares_acquisition_block(model: str) -> None:
    _, filename = MODEL_REGISTRY[model]
    profile = _load_profile(filename)
    acq = profile["capability"]["acquisition"]

    ch = acq["channel"]
    assert set(ch["coupling_modes"]) == {"AC", "DC", "GND"}
    assert set(str(u).upper() for u in ch["units"]) == {
        "VOLTAGE", "WATT", "AMPERE", "UNKNOWN"
    }
    # bw_limit must include the 20 MHz limit + an OFF state.
    bwl = {str(b).upper() for b in ch["bw_limit_values"]}
    assert "20M" in bwl and "OFF" in bwl
    # Probe ratios are the documented DS1000Z attenuation set.
    ratios = {float(r) for r in ch["probe_ratios"]}
    for expected in (0.01, 0.1, 1, 10, 100, 1000):
        assert expected in ratios
    # Scale / offset declare min+max bounds.
    assert ch["scale_v_per_div"]["min"] > 0
    assert ch["scale_v_per_div"]["max"] > ch["scale_v_per_div"]["min"]
    assert ch["offset_v"]["min"] < ch["offset_v"]["max"]

    tb = acq["timebase"]
    assert set(str(m).upper() for m in tb["modes"]) == {"MAIN", "XY", "ROLL"}
    assert tb["scale_s_per_div"]["min"] > 0
    assert tb["scale_s_per_div"]["max"] > tb["scale_s_per_div"]["min"]
    assert tb["offset_s"]["min"] < tb["offset_s"]["max"]
