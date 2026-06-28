"""Unit tests for the glitch_list helper — find runs shorter than min width."""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers.glitch_list import glitch_list
from oscilloscope_mcp.helpers.rle import Run


def test_empty_runs_returns_empty() -> None:
    assert glitch_list([], min_width_us=1.0) == []


def test_no_glitches_when_all_above_threshold() -> None:
    runs = [
        Run(t_us=0.0, level=0, dur_us=5.0),
        Run(t_us=5.0, level=1, dur_us=5.0),
        Run(t_us=10.0, level=0, dur_us=5.0),
    ]
    assert glitch_list(runs, min_width_us=5.0) == []


def test_all_runs_are_glitches() -> None:
    runs = [
        Run(t_us=0.0, level=1, dur_us=0.5),
        Run(t_us=0.5, level=0, dur_us=0.3),
        Run(t_us=0.8, level=1, dur_us=0.2),
    ]
    result = glitch_list(runs, min_width_us=1.0)
    assert len(result) == 3
    assert result[0] == {"t_us": 0.0, "level": 1, "dur_us": 0.5, "index": 0}
    assert result[1] == {"t_us": 0.5, "level": 0, "dur_us": 0.3, "index": 1}
    assert result[2] == {"t_us": 0.8, "level": 1, "dur_us": 0.2, "index": 2}


def test_mixed_glitches_and_normal() -> None:
    runs = [
        Run(t_us=0.0, level=0, dur_us=10.0),
        Run(t_us=10.0, level=1, dur_us=0.1),  # glitch
        Run(t_us=10.1, level=0, dur_us=8.0),
        Run(t_us=18.1, level=1, dur_us=0.05),  # glitch
        Run(t_us=18.15, level=0, dur_us=5.0),
    ]
    result = glitch_list(runs, min_width_us=1.0)
    assert len(result) == 2
    assert result[0]["index"] == 1
    assert result[0]["t_us"] == pytest.approx(10.0)
    assert result[0]["dur_us"] == pytest.approx(0.1)
    assert result[1]["index"] == 3
    assert result[1]["dur_us"] == pytest.approx(0.05)


def test_single_run_above_threshold() -> None:
    runs = [Run(t_us=0.0, level=1, dur_us=100.0)]
    assert glitch_list(runs, min_width_us=50.0) == []


def test_single_run_below_threshold() -> None:
    runs = [Run(t_us=0.0, level=1, dur_us=0.01)]
    result = glitch_list(runs, min_width_us=1.0)
    assert len(result) == 1
    assert result[0] == {"t_us": 0.0, "level": 1, "dur_us": 0.01, "index": 0}


def test_boundary_exact_threshold_not_a_glitch() -> None:
    """A run with dur_us exactly equal to min_width_us is NOT a glitch
    (strict less-than comparison)."""
    runs = [Run(t_us=0.0, level=0, dur_us=2.0)]
    assert glitch_list(runs, min_width_us=2.0) == []


def test_preserves_level_info() -> None:
    """Glitch results carry the level of the run."""
    runs = [
        Run(t_us=0.0, level=0, dur_us=0.5),
        Run(t_us=0.5, level=1, dur_us=0.3),
    ]
    result = glitch_list(runs, min_width_us=1.0)
    assert result[0]["level"] == 0
    assert result[1]["level"] == 1
