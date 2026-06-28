"""Peak-detect min/max decimation for long-window visualization.

Downsamples a raw voltage array into an envelope of min/max blocks,
preserving signal extremes for visual rendering while reducing data
volume. Useful for displaying long captures in limited-pixel-width views.
"""

from __future__ import annotations

import math


def envelope_downsample(
    volts: list[float],
    t0_us: float,
    dt_us: float,
    target_points: int = 200,
) -> dict:
    """Downsample by computing min/max envelope over blocks.

    Parameters
    ----------
    volts : list[float]
        Per-sample voltages from a Waveform.
    t0_us : float
        Time of sample index 0 in microseconds.
    dt_us : float
        Per-sample interval in microseconds.
    target_points : int
        Desired number of output envelope blocks (default 200).

    Returns
    -------
    dict
        {"n_blocks": int, "block_size": int,
         "envelope": [{"t_us": float, "min_v": float, "max_v": float}, ...]}

        If len(volts) <= target_points, each "block" is one sample
        (min==max). The time of each block is the time of the first
        sample in that block.
    """
    if not volts:
        return {"n_blocks": 0, "block_size": 0, "envelope": []}

    n = len(volts)

    if n <= target_points:
        # Each sample is its own block
        envelope = []
        for i, v in enumerate(volts):
            t = t0_us + i * dt_us
            envelope.append({"t_us": t, "min_v": v, "max_v": v})
        return {"n_blocks": n, "block_size": 1, "envelope": envelope}

    block_size = math.ceil(n / target_points)
    envelope = []

    i = 0
    while i < n:
        block_end = min(i + block_size, n)
        block = volts[i:block_end]
        t = t0_us + i * dt_us
        envelope.append({
            "t_us": t,
            "min_v": min(block),
            "max_v": max(block),
        })
        i = block_end

    return {
        "n_blocks": len(envelope),
        "block_size": block_size,
        "envelope": envelope,
    }
