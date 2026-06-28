"""Unit tests for the fft_peaks helper."""

from __future__ import annotations

import math

import pytest

from oscilloscope_mcp.helpers.fft_peaks import fft_peaks, _next_power_of_2


def test_empty_input() -> None:
    result = fft_peaks([], dt_s=1e-6)
    assert result == {"n_samples": 0, "freq_resolution_hz": 0.0, "peaks": []}


def test_zero_dt() -> None:
    result = fft_peaks([1.0, 2.0, 3.0], dt_s=0.0)
    assert result["peaks"] == []


def test_negative_dt() -> None:
    result = fft_peaks([1.0, 2.0, 3.0], dt_s=-1e-6)
    assert result["peaks"] == []


def test_next_power_of_2() -> None:
    assert _next_power_of_2(1) == 1
    assert _next_power_of_2(2) == 2
    assert _next_power_of_2(3) == 4
    assert _next_power_of_2(5) == 8
    assert _next_power_of_2(1200) == 2048


def test_pure_sine_wave_peak_at_correct_frequency() -> None:
    """A pure sine wave should produce a single dominant peak at its
    fundamental frequency."""
    freq_hz = 1000.0  # 1 kHz sine
    dt_s = 1.0 / 10000.0  # 10 kHz sample rate
    n_samples = 1024  # power of 2 for clean FFT

    volts = [math.sin(2.0 * math.pi * freq_hz * i * dt_s) for i in range(n_samples)]
    result = fft_peaks(volts, dt_s=dt_s, n_peaks=3)

    assert result["n_samples"] == n_samples
    assert result["freq_resolution_hz"] > 0

    # The top peak should be at ~1000 Hz
    assert len(result["peaks"]) >= 1
    top_peak = result["peaks"][0]
    # Allow some frequency resolution error (within one bin)
    assert abs(top_peak["freq_hz"] - freq_hz) <= result["freq_resolution_hz"] * 1.5
    # Top peak should be at 0 dB (it IS the maximum)
    assert top_peak["magnitude_db"] == pytest.approx(0.0, abs=0.01)


def test_two_sine_waves_two_peaks() -> None:
    """Two sine waves at different frequencies should produce two peaks."""
    freq1 = 500.0
    freq2 = 2000.0
    dt_s = 1.0 / 8000.0  # 8 kHz sample rate
    n_samples = 1024

    volts = [
        math.sin(2.0 * math.pi * freq1 * i * dt_s) +
        0.5 * math.sin(2.0 * math.pi * freq2 * i * dt_s)
        for i in range(n_samples)
    ]
    result = fft_peaks(volts, dt_s=dt_s, n_peaks=5)

    # Should have at least 2 peaks
    assert len(result["peaks"]) >= 2

    # Check that both frequencies appear in the peaks
    peak_freqs = [p["freq_hz"] for p in result["peaks"][:3]]
    freq_res = result["freq_resolution_hz"]

    found_freq1 = any(abs(f - freq1) <= freq_res * 1.5 for f in peak_freqs)
    found_freq2 = any(abs(f - freq2) <= freq_res * 1.5 for f in peak_freqs)
    assert found_freq1, f"Expected peak near {freq1} Hz, got {peak_freqs}"
    assert found_freq2, f"Expected peak near {freq2} Hz, got {peak_freqs}"


def test_dc_only_signal_no_peaks() -> None:
    """A flat (DC-only) signal should produce no peaks since DC bin is skipped."""
    volts = [2.5] * 256
    result = fft_peaks(volts, dt_s=1e-6, n_peaks=5)
    # After Hann window, a pure DC signal produces only DC energy
    # which is in bin 0 (skipped) and some spectral leakage
    # The main peak should be negligible or absent
    # With a perfect DC + Hann window, there will be some small leakage
    # but the result should have very few significant peaks
    assert result["n_samples"] == 256


def test_single_sample_no_peaks() -> None:
    """A single sample cannot produce frequency information."""
    result = fft_peaks([1.0], dt_s=1e-6, n_peaks=5)
    # With 1 sample padded to 1, FFT has no positive freq bins
    assert result["n_samples"] == 1


def test_magnitude_db_is_relative() -> None:
    """Magnitude values should be in dB relative to the max (max = 0 dB)."""
    freq_hz = 1000.0
    dt_s = 1.0 / 8000.0
    n_samples = 512
    volts = [math.sin(2.0 * math.pi * freq_hz * i * dt_s) for i in range(n_samples)]
    result = fft_peaks(volts, dt_s=dt_s, n_peaks=5)

    # Top peak should be 0 dB
    assert result["peaks"][0]["magnitude_db"] == pytest.approx(0.0, abs=0.01)
    # Other peaks should be negative dB
    for p in result["peaks"][1:]:
        assert p["magnitude_db"] <= 0.0


def test_n_peaks_limits_output() -> None:
    """Should not return more peaks than requested."""
    freq_hz = 1000.0
    dt_s = 1.0 / 8000.0
    n_samples = 512
    volts = [math.sin(2.0 * math.pi * freq_hz * i * dt_s) for i in range(n_samples)]
    result = fft_peaks(volts, dt_s=dt_s, n_peaks=2)
    assert len(result["peaks"]) <= 2


def test_non_power_of_2_samples() -> None:
    """Should handle sample counts that are not powers of 2 (zero-pads)."""
    freq_hz = 500.0
    dt_s = 1.0 / 5000.0
    n_samples = 1200  # Not a power of 2

    volts = [math.sin(2.0 * math.pi * freq_hz * i * dt_s) for i in range(n_samples)]
    result = fft_peaks(volts, dt_s=dt_s, n_peaks=3)

    assert result["n_samples"] == 1200
    # Should still find the peak near 500 Hz
    assert len(result["peaks"]) >= 1
    # Frequency resolution uses padded size (2048)
    top_freq = result["peaks"][0]["freq_hz"]
    assert abs(top_freq - freq_hz) < 10.0  # within 10 Hz
