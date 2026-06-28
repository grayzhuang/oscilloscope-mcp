"""Unit tests for the causality_check helper."""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers.edges import Edge, RISE, FALL
from oscilloscope_mcp.helpers.causality_check import causality_check


def _make_edges(times: list[float], channel: str, kind: str) -> list[Edge]:
    """Helper to create a list of edges at given times."""
    return [Edge(t_us=t, channel=channel, kind=kind) for t in times]


def test_empty_edges_a() -> None:
    edges_b = _make_edges([1.0, 2.0], "CHAN2", RISE)
    result = causality_check([], edges_b, max_delay_us=10.0)
    assert result == {
        "total_a": 0,
        "matched": 0,
        "violations": 0,
        "violation_details": [],
    }


def test_empty_edges_b() -> None:
    edges_a = _make_edges([1.0, 2.0], "CHAN1", RISE)
    result = causality_check(edges_a, [], max_delay_us=10.0)
    assert result["total_a"] == 2
    assert result["matched"] == 0
    assert result["violations"] == 2
    assert len(result["violation_details"]) == 2
    # No B edges means nearest_b_t_us is None
    for v in result["violation_details"]:
        assert v["nearest_b_t_us"] is None
        assert v["delay_us"] is None


def test_both_empty() -> None:
    result = causality_check([], [], max_delay_us=10.0)
    assert result == {
        "total_a": 0,
        "matched": 0,
        "violations": 0,
        "violation_details": [],
    }


def test_all_matched_within_delay() -> None:
    """Every A-RISE is followed by a B-RISE within the allowed window."""
    edges_a = _make_edges([10.0, 20.0, 30.0], "CHAN1", RISE)
    edges_b = _make_edges([12.0, 22.0, 32.0], "CHAN2", RISE)
    result = causality_check(edges_a, edges_b, max_delay_us=5.0)
    assert result["total_a"] == 3
    assert result["matched"] == 3
    assert result["violations"] == 0
    assert result["violation_details"] == []


def test_some_violations() -> None:
    """Second A edge has no B response within the window."""
    edges_a = _make_edges([10.0, 20.0, 30.0], "CHAN1", RISE)
    # B responds to first and third, but not second (gap > max_delay)
    edges_b = _make_edges([12.0, 32.0], "CHAN2", RISE)
    result = causality_check(edges_a, edges_b, max_delay_us=5.0)
    assert result["total_a"] == 3
    assert result["matched"] == 2
    assert result["violations"] == 1
    # The violation is at t=20, nearest B is at t=32 (delay=12 > 5)
    v = result["violation_details"][0]
    assert v["a_t_us"] == 20.0
    assert v["nearest_b_t_us"] == 32.0
    assert v["delay_us"] == pytest.approx(12.0)


def test_exact_boundary_matched() -> None:
    """A B edge exactly at max_delay_us should be matched (<=)."""
    edges_a = _make_edges([10.0], "CHAN1", RISE)
    edges_b = _make_edges([15.0], "CHAN2", RISE)
    result = causality_check(edges_a, edges_b, max_delay_us=5.0)
    assert result["matched"] == 1
    assert result["violations"] == 0


def test_b_before_a_not_matched() -> None:
    """A B edge that occurs BEFORE the A edge should not count."""
    edges_a = _make_edges([10.0], "CHAN1", RISE)
    edges_b = _make_edges([5.0], "CHAN2", RISE)  # Before A
    result = causality_check(edges_a, edges_b, max_delay_us=10.0)
    assert result["matched"] == 0
    assert result["violations"] == 1
    v = result["violation_details"][0]
    assert v["nearest_b_t_us"] is None


def test_kind_filtering() -> None:
    """Only edges matching a_kind and b_kind are considered."""
    # A has both RISE and FALL edges
    edges_a = [
        Edge(t_us=10.0, channel="CHAN1", kind=RISE),
        Edge(t_us=15.0, channel="CHAN1", kind=FALL),
        Edge(t_us=20.0, channel="CHAN1", kind=RISE),
    ]
    # B has only FALL edges
    edges_b = [
        Edge(t_us=11.0, channel="CHAN2", kind=FALL),
        Edge(t_us=21.0, channel="CHAN2", kind=FALL),
    ]
    # Check RISE on A → FALL on B
    result = causality_check(edges_a, edges_b, max_delay_us=5.0,
                             a_kind="RISE", b_kind="FALL")
    assert result["total_a"] == 2  # Only RISE edges on A
    assert result["matched"] == 2  # Both have a FALL on B within 5 us


def test_fall_to_rise_causality() -> None:
    """Can check FALL on A → RISE on B."""
    edges_a = [
        Edge(t_us=5.0, channel="CHAN1", kind=FALL),
        Edge(t_us=15.0, channel="CHAN1", kind=FALL),
    ]
    edges_b = [
        Edge(t_us=7.0, channel="CHAN2", kind=RISE),
        Edge(t_us=25.0, channel="CHAN2", kind=RISE),  # Too late for second
    ]
    result = causality_check(edges_a, edges_b, max_delay_us=5.0,
                             a_kind="FALL", b_kind="RISE")
    assert result["total_a"] == 2
    assert result["matched"] == 1
    assert result["violations"] == 1
