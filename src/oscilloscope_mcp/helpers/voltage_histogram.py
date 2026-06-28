"""Per-channel voltage histogram for diagnosing floating/undefined levels.

Computes a voltage distribution over the raw sample array, useful for
identifying bimodal noise, floating pins, or undefined logic levels.
The histogram bins are evenly spaced between min and max voltage; peaks
are detected as bins with count > mean + 2*stddev of bin counts.
"""

from __future__ import annotations

import math


def voltage_histogram(volts: list[float], n_bins: int = 50) -> dict:
    """Compute a voltage histogram over the raw sample array.

    Parameters
    ----------
    volts : list[float]
        Per-sample voltages from a Waveform.
    n_bins : int
        Number of histogram bins (default 50).

    Returns
    -------
    dict
        {"n_samples": int, "min_v": float, "max_v": float,
         "bins": [{"lo_v": float, "hi_v": float, "count": int}, ...],
         "peaks": [{"center_v": float, "count": int}, ...]}

        ``peaks`` = bins with count > mean + 2*stddev of bin counts
        (simple peak detection).
        If volts is empty, returns {"n_samples": 0, "bins": [], "peaks": []}.
    """
    if not volts:
        return {"n_samples": 0, "bins": [], "peaks": []}

    n_samples = len(volts)
    min_v = min(volts)
    max_v = max(volts)

    # Handle flat signal: all samples at same voltage
    if min_v == max_v:
        bins = [{"lo_v": min_v, "hi_v": max_v, "count": n_samples}]
        peaks = [{"center_v": min_v, "count": n_samples}]
        return {
            "n_samples": n_samples,
            "min_v": min_v,
            "max_v": max_v,
            "bins": bins,
            "peaks": peaks,
        }

    bin_width = (max_v - min_v) / n_bins
    counts = [0] * n_bins

    for v in volts:
        idx = int((v - min_v) / bin_width)
        # Clamp the last value (v == max_v) into the final bin
        if idx >= n_bins:
            idx = n_bins - 1
        counts[idx] += 1

    bins = []
    for i in range(n_bins):
        lo = min_v + i * bin_width
        hi = min_v + (i + 1) * bin_width
        bins.append({"lo_v": lo, "hi_v": hi, "count": counts[i]})

    # Peak detection: bins with count > mean + 2*stddev
    mean_count = sum(counts) / n_bins
    variance = sum((c - mean_count) ** 2 for c in counts) / n_bins
    stddev = math.sqrt(variance)
    threshold = mean_count + 2 * stddev

    peaks = []
    for i, c in enumerate(counts):
        if c > threshold:
            center = min_v + (i + 0.5) * bin_width
            peaks.append({"center_v": center, "count": c})

    return {
        "n_samples": n_samples,
        "min_v": min_v,
        "max_v": max_v,
        "bins": bins,
        "peaks": peaks,
    }
