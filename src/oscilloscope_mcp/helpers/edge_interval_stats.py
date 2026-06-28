"""Compute interval statistics between consecutive same-kind edges.

Useful for jitter analysis, clock stability assessment, and protocol timing
verification.  Operates on the ``Edge`` named-tuples produced by
:func:`oscilloscope_mcp.helpers.edges.edges_from_runs`.

Uses only the Python standard library (math, statistics).
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

from oscilloscope_mcp.helpers.edges import Edge


def edge_interval_stats(edges: Sequence[Edge], kind: str = "RISE") -> dict:
    """Compute statistics of intervals between consecutive edges of *kind*.

    Parameters
    ----------
    edges : sequence of Edge
        Time-ordered edges as produced by
        :func:`oscilloscope_mcp.helpers.edges.edges_from_runs`.
    kind : str
        Edge kind to filter on (``"RISE"`` or ``"FALL"``).

    Returns
    -------
    dict
        Keys: ``count``, ``mean_us``, ``stddev_us``, ``min_us``, ``max_us``,
        ``p99_us``, ``histogram``.

        - ``count`` is the number of intervals (= matched edges - 1).
        - If fewer than 2 matching edges exist, numeric fields are ``None``
          and ``histogram`` is an empty list.
        - ``histogram``: 10 equal-width bins over [min, max], each with
          ``{"lo_us": float, "hi_us": float, "count": int}``.
    """
    # Filter edges matching the requested kind (on any channel).
    matching = [e for e in edges if e.kind == kind]

    if len(matching) < 2:
        return {
            "count": 0,
            "mean_us": None,
            "stddev_us": None,
            "min_us": None,
            "max_us": None,
            "p99_us": None,
            "histogram": [],
        }

    # Compute intervals between consecutive matching edges.
    intervals = [
        matching[i + 1].t_us - matching[i].t_us for i in range(len(matching) - 1)
    ]

    count = len(intervals)
    mean_us = statistics.mean(intervals)
    stddev_us = statistics.pstdev(intervals) if count > 1 else 0.0
    min_us = min(intervals)
    max_us = max(intervals)
    p99_us = _percentile(intervals, 99)

    histogram = _build_histogram(intervals, min_us, max_us, n_bins=10)

    return {
        "count": count,
        "mean_us": mean_us,
        "stddev_us": stddev_us,
        "min_us": min_us,
        "max_us": max_us,
        "p99_us": p99_us,
        "histogram": histogram,
    }


def _percentile(data: list[float], pct: float) -> float:
    """Compute the *pct*-th percentile using the nearest-rank method."""
    sorted_data = sorted(data)
    n = len(sorted_data)
    # Rank index (0-based): ceil(pct/100 * n) - 1, clamped.
    rank = math.ceil(pct / 100.0 * n) - 1
    rank = max(0, min(rank, n - 1))
    return sorted_data[rank]


def _build_histogram(
    data: list[float], lo: float, hi: float, n_bins: int
) -> list[dict]:
    """Build *n_bins* equal-width bins over [lo, hi].

    When lo == hi (all intervals identical), a single bin spanning
    [lo, lo] is returned with count = len(data).
    """
    if lo == hi:
        # Degenerate case: all values identical.
        return [{"lo_us": lo, "hi_us": hi, "count": len(data)}]

    bin_width = (hi - lo) / n_bins
    bins: list[dict] = []
    for i in range(n_bins):
        bin_lo = lo + i * bin_width
        bin_hi = lo + (i + 1) * bin_width
        bins.append({"lo_us": bin_lo, "hi_us": bin_hi, "count": 0})

    for v in data:
        # Determine bin index.
        idx = int((v - lo) / bin_width)
        # Clamp the max value into the last bin.
        if idx >= n_bins:
            idx = n_bins - 1
        bins[idx]["count"] += 1

    return bins
