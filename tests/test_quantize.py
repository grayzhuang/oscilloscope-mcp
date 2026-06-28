"""Unit tests for the quantize helper — Schmitt-trigger hysteresis.

The headline property we care about: on a *noisy* edge, a single
threshold fragments into many micro-pulses, whereas a hysteresis band
wide enough to swallow the noise produces exactly one clean transition.
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers.quantize import peak_to_peak, quantize


def test_empty_input_returns_empty() -> None:
    assert quantize([], 1.5, 0.1) == []


def test_length_matches_input() -> None:
    v = [0.0, 1.0, 2.0, 3.0, 0.0]
    assert len(quantize(v, 1.5, 0.1)) == len(v)


def test_initial_level_from_centre_threshold() -> None:
    # First sample decides initial level by plain >= threshold compare.
    assert quantize([2.0], 1.5, 0.1)[0] == 1
    assert quantize([1.0], 1.5, 0.1)[0] == 0
    # Exactly at threshold counts as high (>=).
    assert quantize([1.5], 1.5, 0.1)[0] == 1


def test_clean_square_wave_no_hysteresis_needed() -> None:
    v = [0.0, 0.0, 3.0, 3.0, 0.0, 0.0]
    assert quantize(v, 1.5, 0.1) == [0, 0, 1, 1, 0, 0]


def test_rising_requires_crossing_upper_threshold() -> None:
    # threshold 1.5, hyst 1.0 → v_high = 2.0, v_low = 1.0.
    # A sample at 1.8 is inside the band: starting low, it must NOT rise.
    v = [0.0, 1.8, 2.1, 1.2, 0.9]
    # 0 -> hold(1.8 < 2.0) -> rise(2.1 >= 2.0) -> hold(1.2 > 1.0) -> fall(0.9 <= 1.0)
    assert quantize(v, 1.5, 1.0) == [0, 0, 1, 1, 0]


def test_hysteresis_suppresses_noisy_edge_fragmentation() -> None:
    """A noisy transition crossing the centre threshold several times
    must produce ONE rising edge with hysteresis, but many with none.
    """
    # Signal ramps up through the threshold while wobbling ±0.2 V.
    noisy = [0.0, 0.0,
             1.4, 1.6, 1.45, 1.62, 1.48,  # wobble around 1.5
             3.0, 3.0, 3.0]
    threshold = 1.5

    # No hysteresis: every wobble across 1.5 toggles the level.
    single = quantize(noisy, threshold, 0.0)
    transitions_single = sum(1 for a, b in zip(single, single[1:]) if a != b)
    assert transitions_single > 1  # fragmented into multiple edges

    # Hysteresis band wide enough (±0.3) to swallow the ±0.2 wobble.
    schmitt = quantize(noisy, threshold, 0.6)  # v_high=1.8, v_low=1.2
    transitions_schmitt = sum(1 for a, b in zip(schmitt, schmitt[1:]) if a != b)
    assert transitions_schmitt == 1  # exactly one clean rising edge
    # And it lands at the real edge (sample index 7, the jump to 3.0).
    assert schmitt == [0, 0, 0, 0, 0, 0, 0, 1, 1, 1]


def test_falling_edge_with_hysteresis() -> None:
    # Start high, wobble down through threshold, hysteresis holds until
    # the signal truly drops below v_low.
    v = [3.0, 3.0, 1.6, 1.4, 1.55, 0.5, 0.0]
    # threshold 1.5, hyst 0.6 → v_high=1.8, v_low=1.2.
    # high held until 0.5 <= 1.2 → falls at index 5.
    assert quantize(v, 1.5, 0.6) == [1, 1, 1, 1, 1, 0, 0]


def test_zero_hysteresis_is_single_threshold() -> None:
    v = [1.0, 1.5, 2.0, 1.4]
    assert quantize(v, 1.5, 0.0) == [0, 1, 1, 0]


def test_negative_hysteresis_rejected() -> None:
    with pytest.raises(ValueError, match="hysteresis_v"):
        quantize([1.0], 1.5, -0.1)


def test_peak_to_peak() -> None:
    assert peak_to_peak([]) == 0.0
    assert peak_to_peak([2.0, 2.0]) == 0.0
    assert peak_to_peak([-1.0, 0.0, 3.0]) == pytest.approx(4.0)
