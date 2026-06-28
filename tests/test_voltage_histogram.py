"""Unit tests for the voltage_histogram helper."""

from __future__ import annotations

import math

import pytest

from oscilloscope_mcp.helpers.voltage_histogram import voltage_histogram


def test_empty_input_returns_zero_structure() -> None:
    result = voltage_histogram([])
    assert result == {"n_samples": 0, "bins": [], "peaks": []}


def test_single_sample() -> None:
    result = voltage_histogram([3.3])
    assert result["n_samples"] == 1
    assert result["min_v"] == 3.3
    assert result["max_v"] == 3.3
    assert len(result["bins"]) == 1
    assert result["bins"][0]["count"] == 1
    assert len(result["peaks"]) == 1


def test_flat_signal_all_same_voltage() -> None:
    volts = [1.8] * 100
    result = voltage_histogram(volts)
    assert result["n_samples"] == 100
    assert result["min_v"] == 1.8
    assert result["max_v"] == 1.8
    # Flat signal: single bin with all counts
    assert len(result["bins"]) == 1
    assert result["bins"][0]["count"] == 100
    # Should detect one peak
    assert len(result["peaks"]) == 1
    assert result["peaks"][0]["center_v"] == 1.8
    assert result["peaks"][0]["count"] == 100


def test_bimodal_signal_detects_two_peaks() -> None:
    """A square-wave-like signal with most samples at 0V or 3.3V should
    produce two distinct peaks."""
    # Create a bimodal distribution: two clusters with some noise spread
    # across the full range. Use enough bins that the clusters land in
    # separate bins with counts clearly above mean + 2*stddev.
    import random
    rng = random.Random(42)
    volts = []
    # 400 samples clustered near 0.1V (within one bin)
    volts += [0.1 + rng.uniform(-0.05, 0.05) for _ in range(400)]
    # 400 samples clustered near 3.2V (within one bin)
    volts += [3.2 + rng.uniform(-0.05, 0.05) for _ in range(400)]
    # 50 samples of uniform noise across 0..3.3 (spread thin across bins)
    volts += [rng.uniform(0.0, 3.3) for _ in range(50)]

    result = voltage_histogram(volts, n_bins=20)

    assert result["n_samples"] == 850
    assert len(result["bins"]) == 20

    # Should detect at least 2 peaks
    assert len(result["peaks"]) >= 2
    # Peaks should be near 0V and 3.3V
    peak_centers = sorted(p["center_v"] for p in result["peaks"])
    assert peak_centers[0] < 1.0  # near 0V cluster
    assert peak_centers[-1] > 2.5  # near 3.3V cluster


def test_uniform_distribution_no_peaks() -> None:
    """A perfectly uniform distribution should have no peaks above
    mean + 2*stddev (since all counts are equal, stddev=0, but we want
    to check the logic handles the case where counts are near-equal)."""
    # Create a signal with exactly equal bin counts
    n_bins = 10
    # 10 samples per bin across 10 bins (perfect uniform)
    volts = []
    for i in range(n_bins):
        volts.extend([i * 0.1 + 0.05] * 10)
    result = voltage_histogram(volts, n_bins=n_bins)
    # With perfectly uniform distribution, stddev of counts is ~0,
    # so threshold = mean + 0 = mean, meaning no bin strictly exceeds it
    # Actually count > threshold (strict), so no peaks if all equal
    assert result["peaks"] == []


def test_bin_boundaries_cover_full_range() -> None:
    volts = [0.0, 1.0, 2.0, 3.0, 4.0]
    result = voltage_histogram(volts, n_bins=4)
    assert result["bins"][0]["lo_v"] == pytest.approx(0.0)
    assert result["bins"][-1]["hi_v"] == pytest.approx(4.0)
    # All samples should be accounted for
    total = sum(b["count"] for b in result["bins"])
    assert total == 5


def test_total_count_equals_n_samples() -> None:
    """Every sample must land in exactly one bin."""
    volts = [0.1 * i for i in range(100)]
    result = voltage_histogram(volts, n_bins=20)
    total = sum(b["count"] for b in result["bins"])
    assert total == result["n_samples"]


def test_custom_n_bins() -> None:
    volts = list(range(100))
    result = voltage_histogram([float(v) for v in volts], n_bins=25)
    assert len(result["bins"]) == 25


def test_negative_voltages() -> None:
    volts = [-2.0, -1.0, 0.0, 1.0, 2.0]
    result = voltage_histogram(volts, n_bins=4)
    assert result["min_v"] == pytest.approx(-2.0)
    assert result["max_v"] == pytest.approx(2.0)
    total = sum(b["count"] for b in result["bins"])
    assert total == 5
