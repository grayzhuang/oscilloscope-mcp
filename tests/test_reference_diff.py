"""Thorough pure-function tests for helpers/reference_diff.py.

These tests exercise the P4 core algorithm in isolation — no hardware,
no MCP, just Edge lists in and a structured diff out.
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers.edges import Edge, RISE, FALL
from oscilloscope_mcp.helpers.rle import Run
from oscilloscope_mcp.helpers.reference_diff import reference_diff, runs_to_edges


# ---------------------------------------------------------------------------
# runs_to_edges convenience wrapper
# ---------------------------------------------------------------------------


def test_runs_to_edges_produces_correct_edges() -> None:
    """runs_to_edges wraps edges_from_runs correctly."""
    runs = [
        Run(t_us=0.0, level=0, dur_us=5.0),
        Run(t_us=5.0, level=1, dur_us=5.0),
        Run(t_us=10.0, level=0, dur_us=5.0),
    ]
    edges = runs_to_edges(runs, channel="SIM")
    assert len(edges) == 2
    assert edges[0] == Edge(t_us=5.0, channel="SIM", kind=RISE)
    assert edges[1] == Edge(t_us=10.0, channel="SIM", kind=FALL)


def test_runs_to_edges_empty_input() -> None:
    """Empty runs produce no edges."""
    assert runs_to_edges([], channel="X") == []


def test_runs_to_edges_single_run_no_edges() -> None:
    """A single run (no transitions) produces no edges."""
    runs = [Run(t_us=0.0, level=1, dur_us=10.0)]
    assert runs_to_edges(runs, channel="X") == []


# ---------------------------------------------------------------------------
# reference_diff — perfect match
# ---------------------------------------------------------------------------


def test_perfect_match_identical_edges() -> None:
    """Identical edge lists -> matched=N, all shifted with delta=0,
    missing=[], added=[], first_divergence=None."""
    edges = [
        Edge(t_us=1.0, channel="CH", kind=RISE),
        Edge(t_us=3.0, channel="CH", kind=FALL),
        Edge(t_us=5.0, channel="CH", kind=RISE),
    ]
    result = reference_diff(edges, list(edges), tolerance_us=0.05)
    assert result["matched"] == 3
    assert len(result["shifted"]) == 3
    assert all(s["delta_us"] == 0.0 for s in result["shifted"])
    assert result["missing"] == []
    assert result["added"] == []
    assert result["first_divergence_us"] is None
    assert "Perfect match" in result["summary"]


def test_perfect_match_empty_inputs() -> None:
    """Both empty -> perfect match (0 edges matched)."""
    result = reference_diff([], [], tolerance_us=1.0)
    assert result["matched"] == 0
    assert result["shifted"] == []
    assert result["missing"] == []
    assert result["added"] == []
    assert result["first_divergence_us"] is None


# ---------------------------------------------------------------------------
# reference_diff — shifted edges
# ---------------------------------------------------------------------------


def test_shifted_edges_within_tolerance() -> None:
    """Edges offset by less than tolerance -> all matched with correct deltas."""
    ref = [
        Edge(t_us=1.0, channel="CH", kind=RISE),
        Edge(t_us=5.0, channel="CH", kind=FALL),
    ]
    hw = [
        Edge(t_us=1.02, channel="CH", kind=RISE),
        Edge(t_us=4.97, channel="CH", kind=FALL),
    ]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 2
    assert result["missing"] == []
    assert result["added"] == []
    assert result["first_divergence_us"] is None

    # Check deltas.
    shifts = {s["kind"]: s["delta_us"] for s in result["shifted"]}
    assert shifts[RISE] == pytest.approx(0.02, abs=1e-6)
    assert shifts[FALL] == pytest.approx(-0.03, abs=1e-6)


def test_shifted_preserves_ref_and_hw_times() -> None:
    """shifted entries report both ref_t_us and hw_t_us."""
    ref = [Edge(t_us=10.0, channel="CH", kind=RISE)]
    hw = [Edge(t_us=10.03, channel="CH", kind=RISE)]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["shifted"][0]["ref_t_us"] == 10.0
    assert result["shifted"][0]["hw_t_us"] == 10.03


# ---------------------------------------------------------------------------
# reference_diff — missing edges
# ---------------------------------------------------------------------------


def test_missing_edge_ref_has_no_hw_match() -> None:
    """Ref has an edge that hw doesn't -> appears in missing."""
    ref = [
        Edge(t_us=1.0, channel="CH", kind=RISE),
        Edge(t_us=5.0, channel="CH", kind=FALL),
    ]
    hw = [
        Edge(t_us=1.0, channel="CH", kind=RISE),
        # No FALL at t=5
    ]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 1
    assert len(result["missing"]) == 1
    assert result["missing"][0] == {"t_us": 5.0, "kind": FALL}
    assert result["added"] == []
    assert result["first_divergence_us"] == 5.0


def test_hw_empty_all_ref_edges_are_missing() -> None:
    """HW has no edges -> all ref edges are missing."""
    ref = [
        Edge(t_us=1.0, channel="CH", kind=RISE),
        Edge(t_us=3.0, channel="CH", kind=FALL),
    ]
    result = reference_diff(ref, [], tolerance_us=1.0)
    assert result["matched"] == 0
    assert len(result["missing"]) == 2
    assert result["added"] == []
    assert result["first_divergence_us"] == 1.0


# ---------------------------------------------------------------------------
# reference_diff — added edges
# ---------------------------------------------------------------------------


def test_added_edge_hw_has_extra() -> None:
    """HW has an extra edge -> appears in added."""
    ref = [Edge(t_us=1.0, channel="CH", kind=RISE)]
    hw = [
        Edge(t_us=1.0, channel="CH", kind=RISE),
        Edge(t_us=7.0, channel="CH", kind=FALL),
    ]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 1
    assert result["missing"] == []
    assert len(result["added"]) == 1
    assert result["added"][0] == {"t_us": 7.0, "kind": FALL}
    assert result["first_divergence_us"] == 7.0


def test_ref_empty_all_hw_edges_are_added() -> None:
    """Ref has no edges -> all hw edges are added."""
    hw = [
        Edge(t_us=2.0, channel="CH", kind=RISE),
        Edge(t_us=4.0, channel="CH", kind=FALL),
    ]
    result = reference_diff([], hw, tolerance_us=1.0)
    assert result["matched"] == 0
    assert result["missing"] == []
    assert len(result["added"]) == 2
    assert result["first_divergence_us"] == 2.0


# ---------------------------------------------------------------------------
# reference_diff — mixed scenario
# ---------------------------------------------------------------------------


def test_mixed_shifted_missing_and_added() -> None:
    """Some shifted, some missing, some added."""
    ref = [
        Edge(t_us=1.0, channel="CH", kind=RISE),   # matched
        Edge(t_us=5.0, channel="CH", kind=FALL),   # missing (no hw FALL near 5)
        Edge(t_us=9.0, channel="CH", kind=RISE),   # matched
    ]
    hw = [
        Edge(t_us=1.01, channel="CH", kind=RISE),  # matches ref@1.0
        Edge(t_us=3.0, channel="CH", kind=FALL),   # added (no ref FALL near 3)
        Edge(t_us=9.02, channel="CH", kind=RISE),  # matches ref@9.0
    ]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 2
    assert len(result["missing"]) == 1
    assert result["missing"][0]["t_us"] == 5.0
    assert len(result["added"]) == 1
    assert result["added"][0]["t_us"] == 3.0
    # First divergence is the earliest among missing + added.
    assert result["first_divergence_us"] == 3.0


# ---------------------------------------------------------------------------
# reference_diff — first_divergence
# ---------------------------------------------------------------------------


def test_first_divergence_picks_earliest_mismatch() -> None:
    """first_divergence_us is the minimum time among all mismatches."""
    ref = [
        Edge(t_us=2.0, channel="CH", kind=RISE),
        Edge(t_us=8.0, channel="CH", kind=FALL),  # missing
    ]
    hw = [
        Edge(t_us=2.0, channel="CH", kind=RISE),
        Edge(t_us=4.0, channel="CH", kind=RISE),  # added (earliest mismatch)
    ]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["first_divergence_us"] == 4.0


# ---------------------------------------------------------------------------
# reference_diff — tolerance boundary
# ---------------------------------------------------------------------------


def test_tolerance_boundary_exact_match() -> None:
    """Edge at exactly tolerance distance is included (matched)."""
    ref = [Edge(t_us=10.0, channel="CH", kind=RISE)]
    hw = [Edge(t_us=10.05, channel="CH", kind=RISE)]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 1
    assert result["missing"] == []
    assert result["added"] == []


def test_tolerance_boundary_just_beyond() -> None:
    """Edge just beyond tolerance is excluded (missing + added)."""
    ref = [Edge(t_us=10.0, channel="CH", kind=RISE)]
    hw = [Edge(t_us=10.06, channel="CH", kind=RISE)]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 0
    assert len(result["missing"]) == 1
    assert len(result["added"]) == 1


def test_zero_tolerance_only_exact_matches() -> None:
    """With tolerance=0, only identical times match."""
    ref = [Edge(t_us=1.0, channel="CH", kind=RISE)]
    hw = [Edge(t_us=1.0, channel="CH", kind=RISE)]
    result = reference_diff(ref, hw, tolerance_us=0.0)
    assert result["matched"] == 1

    # Even a tiny offset fails with tolerance=0.
    hw2 = [Edge(t_us=1.001, channel="CH", kind=RISE)]
    result2 = reference_diff(ref, hw2, tolerance_us=0.0)
    assert result2["matched"] == 0


# ---------------------------------------------------------------------------
# reference_diff — kind mismatch
# ---------------------------------------------------------------------------


def test_rise_does_not_match_fall() -> None:
    """RISE in ref doesn't match FALL in hw even if same time."""
    ref = [Edge(t_us=5.0, channel="CH", kind=RISE)]
    hw = [Edge(t_us=5.0, channel="CH", kind=FALL)]
    result = reference_diff(ref, hw, tolerance_us=1.0)
    assert result["matched"] == 0
    assert len(result["missing"]) == 1
    assert len(result["added"]) == 1


def test_multiple_edges_same_kind_greedy_nearest() -> None:
    """With multiple hw edges of the same kind, the nearest one matches."""
    ref = [Edge(t_us=10.0, channel="CH", kind=RISE)]
    hw = [
        Edge(t_us=9.98, channel="CH", kind=RISE),   # distance = 0.02 (closest)
        Edge(t_us=10.04, channel="CH", kind=RISE),  # distance = 0.04
    ]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 1
    assert result["shifted"][0]["hw_t_us"] == 9.98
    # The farther hw edge is "added".
    assert len(result["added"]) == 1
    assert result["added"][0]["t_us"] == 10.04


# ---------------------------------------------------------------------------
# reference_diff — negative tolerance error
# ---------------------------------------------------------------------------


def test_negative_tolerance_raises() -> None:
    """Negative tolerance_us raises ValueError."""
    with pytest.raises(ValueError, match="tolerance_us"):
        reference_diff([], [], tolerance_us=-0.01)


# ---------------------------------------------------------------------------
# reference_diff — channel field is ignored during matching
# ---------------------------------------------------------------------------


def test_channel_label_does_not_affect_matching() -> None:
    """Channel labels differ between ref/hw but matching works on kind+time."""
    ref = [Edge(t_us=1.0, channel="SIM_CH1", kind=RISE)]
    hw = [Edge(t_us=1.01, channel="CHAN1", kind=RISE)]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 1


# ---------------------------------------------------------------------------
# reference_diff — summary string
# ---------------------------------------------------------------------------


def test_summary_string_perfect() -> None:
    """Perfect match summary mentions 'Perfect match'."""
    ref = [Edge(t_us=1.0, channel="CH", kind=RISE)]
    result = reference_diff(ref, list(ref), tolerance_us=0.1)
    assert "Perfect match" in result["summary"]


def test_summary_string_with_mismatches() -> None:
    """Non-perfect summary mentions missing/added counts."""
    ref = [Edge(t_us=1.0, channel="CH", kind=RISE)]
    result = reference_diff(ref, [], tolerance_us=0.1)
    assert "missing" in result["summary"]
    assert "0/1" in result["summary"]


# ---------------------------------------------------------------------------
# reference_diff — ordering invariance
# ---------------------------------------------------------------------------


def test_unsorted_inputs_still_work() -> None:
    """Inputs don't need to be pre-sorted."""
    ref = [
        Edge(t_us=5.0, channel="CH", kind=FALL),
        Edge(t_us=1.0, channel="CH", kind=RISE),
    ]
    hw = [
        Edge(t_us=5.01, channel="CH", kind=FALL),
        Edge(t_us=0.99, channel="CH", kind=RISE),
    ]
    result = reference_diff(ref, hw, tolerance_us=0.05)
    assert result["matched"] == 2
    assert result["missing"] == []
    assert result["added"] == []
