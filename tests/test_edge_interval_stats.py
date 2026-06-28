"""Unit tests for the edge_interval_stats helper — interval statistics."""

from __future__ import annotations

import math

import pytest

from oscilloscope_mcp.helpers.edge_interval_stats import edge_interval_stats
from oscilloscope_mcp.helpers.edges import Edge


def test_empty_edges_returns_count_zero() -> None:
    result = edge_interval_stats([], kind="RISE")
    assert result["count"] == 0
    assert result["mean_us"] is None
    assert result["stddev_us"] is None
    assert result["min_us"] is None
    assert result["max_us"] is None
    assert result["p99_us"] is None
    assert result["histogram"] == []


def test_single_rise_edge_returns_count_zero() -> None:
    """Need at least 2 matching edges to compute an interval."""
    edges = [Edge(t_us=1.0, channel="CHAN1", kind="RISE")]
    result = edge_interval_stats(edges, kind="RISE")
    assert result["count"] == 0


def test_only_fall_edges_when_filtering_rise() -> None:
    """No RISE edges → count 0."""
    edges = [
        Edge(t_us=1.0, channel="CHAN1", kind="FALL"),
        Edge(t_us=3.0, channel="CHAN1", kind="FALL"),
        Edge(t_us=5.0, channel="CHAN1", kind="FALL"),
    ]
    result = edge_interval_stats(edges, kind="RISE")
    assert result["count"] == 0


def test_exactly_two_rise_edges() -> None:
    """Two edges produce exactly one interval."""
    edges = [
        Edge(t_us=0.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=10.0, channel="CHAN1", kind="RISE"),
    ]
    result = edge_interval_stats(edges, kind="RISE")
    assert result["count"] == 1
    assert result["mean_us"] == pytest.approx(10.0)
    assert result["stddev_us"] == pytest.approx(0.0)
    assert result["min_us"] == pytest.approx(10.0)
    assert result["max_us"] == pytest.approx(10.0)
    assert result["p99_us"] == pytest.approx(10.0)
    # Histogram: single bin with all values identical.
    assert len(result["histogram"]) == 1
    assert result["histogram"][0]["count"] == 1


def test_many_rise_edges_uniform_spacing() -> None:
    """Uniformly spaced edges → stddev = 0, all intervals equal."""
    edges = [Edge(t_us=float(i * 5), channel="CHAN1", kind="RISE") for i in range(11)]
    result = edge_interval_stats(edges, kind="RISE")
    assert result["count"] == 10
    assert result["mean_us"] == pytest.approx(5.0)
    assert result["stddev_us"] == pytest.approx(0.0)
    assert result["min_us"] == pytest.approx(5.0)
    assert result["max_us"] == pytest.approx(5.0)


def test_varied_intervals_statistics() -> None:
    """Verify mean, stddev, min, max with known values."""
    # Intervals: 2, 4, 6, 8 → mean=5, pstddev=sqrt(5)
    edges = [
        Edge(t_us=0.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=2.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=6.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=12.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=20.0, channel="CHAN1", kind="RISE"),
    ]
    result = edge_interval_stats(edges, kind="RISE")
    assert result["count"] == 4
    assert result["mean_us"] == pytest.approx(5.0)
    assert result["stddev_us"] == pytest.approx(math.sqrt(5.0))
    assert result["min_us"] == pytest.approx(2.0)
    assert result["max_us"] == pytest.approx(8.0)


def test_histogram_has_10_bins() -> None:
    """Histogram should have 10 bins when min != max."""
    edges = [
        Edge(t_us=0.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=1.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=3.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=10.0, channel="CHAN1", kind="RISE"),
    ]
    result = edge_interval_stats(edges, kind="RISE")
    assert len(result["histogram"]) == 10
    # Total counts in histogram must equal count.
    total = sum(b["count"] for b in result["histogram"])
    assert total == result["count"]


def test_histogram_bin_boundaries() -> None:
    """First bin starts at min, last bin ends at max."""
    edges = [
        Edge(t_us=0.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=2.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=12.0, channel="CHAN1", kind="RISE"),
    ]
    result = edge_interval_stats(edges, kind="RISE")
    hist = result["histogram"]
    assert hist[0]["lo_us"] == pytest.approx(2.0)
    assert hist[-1]["hi_us"] == pytest.approx(10.0)


def test_filter_fall_edges() -> None:
    """Filtering by FALL works correctly, ignoring RISE edges."""
    edges = [
        Edge(t_us=0.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=1.0, channel="CHAN1", kind="FALL"),
        Edge(t_us=2.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=4.0, channel="CHAN1", kind="FALL"),
        Edge(t_us=5.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=9.0, channel="CHAN1", kind="FALL"),
    ]
    result = edge_interval_stats(edges, kind="FALL")
    # FALL edges at 1, 4, 9 → intervals 3, 5 → mean=4, count=2
    assert result["count"] == 2
    assert result["mean_us"] == pytest.approx(4.0)
    assert result["min_us"] == pytest.approx(3.0)
    assert result["max_us"] == pytest.approx(5.0)


def test_multi_channel_edges_all_included() -> None:
    """Edges from different channels are all included in the computation."""
    edges = [
        Edge(t_us=0.0, channel="CHAN1", kind="RISE"),
        Edge(t_us=5.0, channel="CHAN2", kind="RISE"),
        Edge(t_us=10.0, channel="CHAN1", kind="RISE"),
    ]
    result = edge_interval_stats(edges, kind="RISE")
    # All RISE edges regardless of channel: intervals 5, 5
    assert result["count"] == 2
    assert result["mean_us"] == pytest.approx(5.0)
