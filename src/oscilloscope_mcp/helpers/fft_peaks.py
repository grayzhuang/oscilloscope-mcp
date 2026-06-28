"""Top-N frequency peaks from raw voltage samples via FFT.

Used for oscillator purity analysis, EMI identification, and switching
noise characterization. Implements a radix-2 Cooley-Tukey FFT using only
stdlib (cmath/math) -- no numpy/scipy dependency.
"""

from __future__ import annotations

import cmath
import math


def _next_power_of_2(n: int) -> int:
    """Return the smallest power of 2 >= n."""
    if n <= 1:
        return 1
    return 1 << (n - 1).bit_length()


def _fft_radix2(x: list[complex]) -> list[complex]:
    """In-place iterative radix-2 Cooley-Tukey FFT.

    ``x`` must have length that is a power of 2.
    Returns the DFT of x.
    """
    n = len(x)
    if n == 1:
        return x

    # Bit-reversal permutation
    bits = n.bit_length() - 1
    for i in range(n):
        j = int(bin(i)[2:].zfill(bits)[::-1], 2)
        if i < j:
            x[i], x[j] = x[j], x[i]

    # Butterfly stages
    length = 2
    while length <= n:
        half = length // 2
        w_base = -2.0 * math.pi / length
        for start in range(0, n, length):
            for k in range(half):
                w = cmath.exp(complex(0, w_base * k))
                even = x[start + k]
                odd = x[start + k + half] * w
                x[start + k] = even + odd
                x[start + k + half] = even - odd
        length *= 2

    return x


def fft_peaks(volts: list[float], dt_s: float, n_peaks: int = 5) -> dict:
    """Compute FFT magnitude spectrum and return top-N peaks.

    Parameters
    ----------
    volts : list[float]
        Per-sample voltages from a Waveform.
    dt_s : float
        Per-sample interval in seconds (from Waveform.dt_s).
    n_peaks : int
        Number of top peaks to return (default 5).

    Returns
    -------
    dict
        {"n_samples": int, "freq_resolution_hz": float,
         "peaks": [{"freq_hz": float, "magnitude_db": float}, ...]}

        Peaks sorted by magnitude descending. Magnitude in dB relative
        to max magnitude. Skip DC (bin 0). Only positive frequencies
        (first half of spectrum).
    """
    if not volts or dt_s <= 0:
        return {"n_samples": len(volts) if volts else 0,
                "freq_resolution_hz": 0.0, "peaks": []}

    n_orig = len(volts)
    n_fft = _next_power_of_2(n_orig)

    # Apply Hann window
    windowed: list[complex] = []
    for i in range(n_orig):
        w = 0.5 * (1.0 - math.cos(2.0 * math.pi * i / n_orig))
        windowed.append(complex(volts[i] * w, 0.0))

    # Zero-pad to next power of 2
    windowed.extend([complex(0, 0)] * (n_fft - n_orig))

    # Compute FFT
    spectrum = _fft_radix2(windowed)

    # Compute magnitudes for positive frequencies only (skip DC bin 0)
    freq_resolution = 1.0 / (n_fft * dt_s)
    n_positive = n_fft // 2  # bins 1..n_fft//2-1 are positive freqs

    magnitudes: list[tuple[int, float]] = []  # (bin_index, magnitude)
    for i in range(1, n_positive):
        mag = abs(spectrum[i])
        if mag > 0:
            magnitudes.append((i, mag))

    if not magnitudes:
        return {"n_samples": n_orig, "freq_resolution_hz": freq_resolution,
                "peaks": []}

    # Find max magnitude for dB reference
    max_mag = max(m for _, m in magnitudes)

    # Convert to dB relative to max
    mag_db: list[tuple[int, float]] = []
    for bin_idx, mag in magnitudes:
        db = 20.0 * math.log10(mag / max_mag) if mag > 0 else -200.0
        mag_db.append((bin_idx, db))

    # Sort by magnitude (descending = least negative dB first)
    mag_db.sort(key=lambda x: x[1], reverse=True)

    # Take top N peaks
    peaks = []
    for bin_idx, db in mag_db[:n_peaks]:
        freq_hz = bin_idx * freq_resolution
        peaks.append({"freq_hz": freq_hz, "magnitude_db": round(db, 2)})

    return {
        "n_samples": n_orig,
        "freq_resolution_hz": freq_resolution,
        "peaks": peaks,
    }
