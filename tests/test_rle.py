"""Unit tests for the RLE helper — digital levels → time-stamped runs."""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers.rle import Run, runs_from_levels


def test_empty_levels_returns_empty() -> None:
    assert runs_from_levels([], t0_us=0.0, dt_us=1.0) == []


def test_single_run_spans_whole_buffer() -> None:
    runs = runs_from_levels([0, 0, 0, 0], t0_us=0.0, dt_us=2.0)
    assert runs == [Run(t_us=0.0, level=0, dur_us=8.0)]


def test_three_runs_tile_without_gaps() -> None:
    levels = [0, 0, 0, 1, 1, 0, 0]
    runs = runs_from_levels(levels, t0_us=0.0, dt_us=1.0)
    assert runs == [
        Run(t_us=0.0, level=0, dur_us=3.0),
        Run(t_us=3.0, level=1, dur_us=2.0),
        Run(t_us=5.0, level=0, dur_us=2.0),
    ]
    # Start times tile: each run starts where the previous ends.
    for prev, cur in zip(runs, runs[1:]):
        assert cur.t_us == pytest.approx(prev.t_us + prev.dur_us)


def test_time_origin_offset_applied() -> None:
    # Trigger at t=0 means pre-trigger samples have negative t_us.
    runs = runs_from_levels([1, 1, 0, 0], t0_us=-2.0, dt_us=1.0)
    assert runs[0].t_us == pytest.approx(-2.0)
    assert runs[1].t_us == pytest.approx(0.0)


def test_alternating_levels_one_run_each() -> None:
    runs = runs_from_levels([0, 1, 0, 1], t0_us=0.0, dt_us=0.5)
    assert [r.level for r in runs] == [0, 1, 0, 1]
    assert all(r.dur_us == pytest.approx(0.5) for r in runs)


def test_rounding_keeps_json_compact() -> None:
    runs = runs_from_levels([0, 1], t0_us=0.0, dt_us=1.0 / 3.0, round_ndigits=4)
    # 1/3 µs rounded to 4 places.
    assert runs[0].dur_us == 0.3333


def test_non_positive_dt_rejected() -> None:
    with pytest.raises(ValueError, match="dt_us"):
        runs_from_levels([0, 1], t0_us=0.0, dt_us=0.0)


def test_clean_square_wave_collapses_to_few_runs() -> None:
    """The DoD motivation: a 1200-sample clean square wave RLE-collapses
    to a small number of runs (well under any size budget)."""
    # 6 full periods, 200 samples per period (100 low / 100 high).
    levels: list[int] = []
    for _ in range(6):
        levels += [0] * 100 + [1] * 100
    runs = runs_from_levels(levels, t0_us=0.0, dt_us=1.0)
    assert len(levels) == 1200
    assert len(runs) == 12  # 6 periods × (one low + one high) run
