"""Unit tests for the pattern_search helper — bit-pattern template matching."""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers.pattern_search import pattern_search
from oscilloscope_mcp.helpers.rle import Run


def test_empty_runs_returns_empty() -> None:
    assert pattern_search([], [0, 1]) == []


def test_empty_pattern_returns_empty() -> None:
    runs = [Run(t_us=0.0, level=0, dur_us=5.0)]
    assert pattern_search(runs, []) == []


def test_pattern_longer_than_runs() -> None:
    runs = [Run(t_us=0.0, level=0, dur_us=5.0)]
    assert pattern_search(runs, [0, 1, 0]) == []


def test_single_match() -> None:
    runs = [
        Run(t_us=0.0, level=0, dur_us=3.0),
        Run(t_us=3.0, level=1, dur_us=2.0),
        Run(t_us=5.0, level=0, dur_us=4.0),
    ]
    result = pattern_search(runs, [0, 1, 0])
    assert len(result) == 1
    assert result[0] == {"index": 0, "t_us": 0.0, "dur_us": 9.0}


def test_multiple_matches() -> None:
    # Pattern [0, 1] occurs at indices 0 and 2.
    runs = [
        Run(t_us=0.0, level=0, dur_us=2.0),
        Run(t_us=2.0, level=1, dur_us=2.0),
        Run(t_us=4.0, level=0, dur_us=2.0),
        Run(t_us=6.0, level=1, dur_us=2.0),
    ]
    result = pattern_search(runs, [0, 1])
    assert len(result) == 2
    assert result[0] == {"index": 0, "t_us": 0.0, "dur_us": 4.0}
    assert result[1] == {"index": 2, "t_us": 4.0, "dur_us": 4.0}


def test_no_match() -> None:
    runs = [
        Run(t_us=0.0, level=0, dur_us=5.0),
        Run(t_us=5.0, level=1, dur_us=5.0),
        Run(t_us=10.0, level=0, dur_us=5.0),
    ]
    # Looking for [1, 1] — never occurs since RLE runs alternate levels.
    result = pattern_search(runs, [1, 1])
    assert result == []


def test_single_element_pattern() -> None:
    runs = [
        Run(t_us=0.0, level=1, dur_us=3.0),
        Run(t_us=3.0, level=0, dur_us=2.0),
        Run(t_us=5.0, level=1, dur_us=4.0),
    ]
    result = pattern_search(runs, [1])
    assert len(result) == 2
    assert result[0] == {"index": 0, "t_us": 0.0, "dur_us": 3.0}
    assert result[1] == {"index": 2, "t_us": 5.0, "dur_us": 4.0}


def test_tolerance_matching_rejects_different_duration() -> None:
    """With tolerance enabled, the first match sets the template.
    Subsequent matches with different durations are rejected."""
    runs = [
        Run(t_us=0.0, level=0, dur_us=5.0),
        Run(t_us=5.0, level=1, dur_us=5.0),
        Run(t_us=10.0, level=0, dur_us=5.0),
        Run(t_us=15.0, level=1, dur_us=20.0),  # too long
        Run(t_us=35.0, level=0, dur_us=5.0),
    ]
    # Pattern [0, 1]: first match at 0 (durations 5, 5).
    # Second candidate at index 2 (durations 5, 20) — dur_us=20 vs template 5.
    # Tolerance=2 means |20-5|=15 > 2 → rejected.
    result = pattern_search(runs, [0, 1], tolerance_us=2.0)
    assert len(result) == 1
    assert result[0]["index"] == 0


def test_tolerance_matching_accepts_within_tolerance() -> None:
    """Matches within tolerance are accepted."""
    runs = [
        Run(t_us=0.0, level=0, dur_us=5.0),
        Run(t_us=5.0, level=1, dur_us=5.0),
        Run(t_us=10.0, level=0, dur_us=5.5),  # within tolerance
        Run(t_us=15.5, level=1, dur_us=4.8),  # within tolerance
        Run(t_us=20.3, level=0, dur_us=5.0),
    ]
    result = pattern_search(runs, [0, 1], tolerance_us=1.0)
    assert len(result) == 2
    assert result[0]["index"] == 0
    assert result[1]["index"] == 2


def test_tolerance_zero_behaves_like_no_tolerance() -> None:
    """tolerance_us=0 means template matching is disabled (level-only)."""
    runs = [
        Run(t_us=0.0, level=0, dur_us=5.0),
        Run(t_us=5.0, level=1, dur_us=5.0),
        Run(t_us=10.0, level=0, dur_us=100.0),
        Run(t_us=110.0, level=1, dur_us=1.0),
    ]
    result = pattern_search(runs, [0, 1], tolerance_us=0.0)
    # Both [0,1] at index 0 and index 2 match (no duration check).
    assert len(result) == 2


def test_pattern_at_end_of_runs() -> None:
    """Pattern can match at the very end of the run list."""
    runs = [
        Run(t_us=0.0, level=1, dur_us=10.0),
        Run(t_us=10.0, level=0, dur_us=3.0),
        Run(t_us=13.0, level=1, dur_us=2.0),
    ]
    result = pattern_search(runs, [0, 1])
    assert len(result) == 1
    assert result[0] == {"index": 1, "t_us": 10.0, "dur_us": 5.0}


def test_overlapping_matches_allowed() -> None:
    """Overlapping matches are possible (sliding window, not consuming)."""
    # Pattern [0, 1, 0] can match at index 0 and index 2 if levels repeat.
    runs = [
        Run(t_us=0.0, level=0, dur_us=1.0),
        Run(t_us=1.0, level=1, dur_us=1.0),
        Run(t_us=2.0, level=0, dur_us=1.0),
        Run(t_us=3.0, level=1, dur_us=1.0),
        Run(t_us=4.0, level=0, dur_us=1.0),
    ]
    result = pattern_search(runs, [0, 1, 0])
    assert len(result) == 2
    assert result[0]["index"] == 0
    assert result[1]["index"] == 2
