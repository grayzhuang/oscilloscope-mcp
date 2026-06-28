"""Unit tests for :mod:`bench.helpers.caveat_calc`."""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers import caveat_calc


_RIGOL_DS1104Z_PROFILE = {
    "model": "RIGOL_DS1104Z",
    "capability": {
        "analog_bw_hz": 100e6,
        "channels": 4,
        "sample_rate_mode_dependent": {"1": 1e9, "2": 500e6, "4": 250e6},
        "memory_depth_pts_total": 24e6,
    },
}


def test_no_caveats_when_single_channel_and_no_signal_hint() -> None:
    caveats = caveat_calc.compute_caveats(
        _RIGOL_DS1104Z_PROFILE, active_channel_count=1
    )
    assert caveats == []


def test_multi_channel_sample_rate_downgrade_emits_caveat() -> None:
    caveats = caveat_calc.compute_caveats(
        _RIGOL_DS1104Z_PROFILE, active_channel_count=4
    )
    assert len(caveats) == 1
    assert "4-channel" in caveats[0]
    assert "250 MSa/s" in caveats[0]
    assert "50 MHz" in caveats[0]  # 250 MSa/s / 5 = 50 MHz practical limit


def test_signal_above_analog_bandwidth_emits_caveat() -> None:
    caveats = caveat_calc.compute_caveats(
        _RIGOL_DS1104Z_PROFILE,
        active_channel_count=1,
        expected_signal_freq_hz=200e6,  # exceeds 100 MHz BW
    )
    assert any("exceeds scope analog BW" in c for c in caveats)


def test_signal_above_practical_sample_rate_limit_emits_caveat() -> None:
    caveats = caveat_calc.compute_caveats(
        _RIGOL_DS1104Z_PROFILE,
        active_channel_count=4,             # 250 MSa/s → 50 MHz practical
        expected_signal_freq_hz=60e6,
    )
    assert any("practical observation limit" in c for c in caveats)


def test_memory_depth_downgrade_emits_caveat() -> None:
    # 10 ms/div × 12 div = 120 ms window; × 250 MSa/s (4-ch) = 30 Mpts required.
    # Per-channel memory budget = 24M / 4 = 6 Mpts → must downgrade.
    caveats = caveat_calc.compute_caveats(
        _RIGOL_DS1104Z_PROFILE,
        active_channel_count=4,
        timebase_s_per_div=10e-3,
    )
    assert any("memory" in c.lower() and "downgraded" in c.lower() for c in caveats)


def test_raw_scpi_caveat_is_non_empty() -> None:
    assert caveat_calc.caveat_for_raw_scpi() != []


def test_cursor_readout_caveat_mentions_precision() -> None:
    msgs = caveat_calc.caveat_for_cursor_readout()
    assert any("pixel" in m.lower() or "resolution" in m.lower() for m in msgs)


def test_waveform_quantize_no_caveats_when_hysteresis_small() -> None:
    # 0.1 V hysteresis on a 3 V p-p signal is 3.3% < 20% → clean.
    msgs = caveat_calc.caveat_for_waveform_quantize(
        hysteresis_v=0.1, peak_to_peak_v=3.0, truncated=False
    )
    assert msgs == []


def test_waveform_quantize_detection_miss_caveat_over_20pct() -> None:
    # 0.8 V hysteresis on a 3 V p-p signal is 26.7% > 20% → detection-miss.
    msgs = caveat_calc.caveat_for_waveform_quantize(
        hysteresis_v=0.8, peak_to_peak_v=3.0, truncated=False
    )
    assert len(msgs) == 1
    assert "hysteresis" in msgs[0].lower()
    assert "missed" in msgs[0].lower()


def test_waveform_quantize_flat_signal_caveat() -> None:
    msgs = caveat_calc.caveat_for_waveform_quantize(
        hysteresis_v=0.1, peak_to_peak_v=0.0, truncated=False
    )
    assert len(msgs) == 1
    assert "flat" in msgs[0].lower()


def test_waveform_quantize_truncation_caveat() -> None:
    msgs = caveat_calc.caveat_for_waveform_quantize(
        hysteresis_v=0.1, peak_to_peak_v=3.0, truncated=True
    )
    assert len(msgs) == 1
    assert "truncat" in msgs[0].lower()


def test_waveform_quantize_both_caveats_combine() -> None:
    msgs = caveat_calc.caveat_for_waveform_quantize(
        hysteresis_v=1.0, peak_to_peak_v=3.0, truncated=True
    )
    assert len(msgs) == 2  # detection-miss + truncation


def test_fmt_sa_unit_selection() -> None:
    assert caveat_calc._fmt_sa(1e9).endswith("GSa/s")
    assert caveat_calc._fmt_sa(250e6).endswith("MSa/s")
    assert caveat_calc._fmt_sa(50e3).endswith("kSa/s")


@pytest.mark.parametrize("ch_count,expected_rate", [
    (1, 1e9),
    (2, 500e6),
    (3, 500e6),  # falls back to nearest lower threshold (2-ch)
    (4, 250e6),
])
def test_sample_rate_for_channels_table_lookup(ch_count: int, expected_rate: float) -> None:
    rate = caveat_calc._sample_rate_for_channels(_RIGOL_DS1104Z_PROFILE, ch_count)
    assert rate == expected_rate
