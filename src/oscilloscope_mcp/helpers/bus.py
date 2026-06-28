"""Time-align per-channel runs into a single multi-bit bus run stream.

Given the RLE ``runs`` of several channels that form a logical bus
(e.g. a 4-bit parallel bus, or DIR/STP/NXT/CLK lines), this collapses
them into ``bus_runs`` — one run per interval during which *no* channel
changes level, carrying the integer bus value over that interval.

Bus value convention
---------------------
Channels are supplied most-significant-first: ``channels_runs[0]`` is the
MSB, the last entry is the LSB. The value during an interval is

    value = Σ  level_i << (n_channels - 1 - i)

so for two channels ``[A, B]`` with A high and B low the value is
``0b10 == 2``.

Algorithm
---------
1. Collect every run-boundary time across all channels (the union of all
   transition instants) → the sorted set of segment boundaries.
2. For each consecutive boundary pair, sample each channel's level at the
   segment start, pack into the bus value, and emit a ``BusRun`` if the
   value differs from the previous emitted one (adjacent equal-value
   segments are merged so the output is itself run-length-encoded).

This is a pure function over the run lists; the driver/tool layer decides
which channels form a bus and in what order.
"""

from __future__ import annotations

from typing import NamedTuple, Sequence

from oscilloscope_mcp.helpers.rle import Run


class BusRun(NamedTuple):
    """One interval of constant multi-bit bus value."""

    t_us: float
    value: int
    dur_us: float


def _level_at(runs: Sequence[Run], t_us: float) -> int:
    """Level of a channel at time ``t_us``: the level of the run whose
    half-open span ``[t, t + dur)`` contains ``t_us``. Before the first
    run we hold the first run's level; after the last, the last run's.
    """
    if not runs:
        return 0
    if t_us < runs[0].t_us:
        return runs[0].level
    for run in runs:
        if run.t_us <= t_us < run.t_us + run.dur_us:
            return run.level
    return runs[-1].level


def bus_runs_from_channels(
    channels_runs: Sequence[Sequence[Run]],
    round_ndigits: int = 4,
) -> list[BusRun]:
    """Time-align ``channels_runs`` (MSB-first) into merged bus runs.

    Parameters
    ----------
    channels_runs : sequence of run-lists
        One RLE run list per channel, MSB first. All lists should share
        the same time base (same capture).
    round_ndigits : int
        Decimal places for ``t_us`` / ``dur_us``.

    Returns
    -------
    list[BusRun]
        Run-length-encoded bus value over time. Empty if no channel has
        any runs.
    """
    non_empty = [r for r in channels_runs if r]
    if not non_empty:
        return []

    n = len(channels_runs)
    # Union of all boundary times: each run start, plus each capture end.
    boundaries: set[float] = set()
    end_time = float("-inf")
    for runs in channels_runs:
        for run in runs:
            boundaries.add(run.t_us)
            end_time = max(end_time, run.t_us + run.dur_us)
    edges = sorted(boundaries)

    out: list[BusRun] = []
    for i, seg_start in enumerate(edges):
        seg_end = edges[i + 1] if i + 1 < len(edges) else end_time
        if seg_end <= seg_start:
            continue
        value = 0
        for idx, runs in enumerate(channels_runs):
            bit = _level_at(runs, seg_start)
            value |= bit << (n - 1 - idx)
        if out and out[-1].value == value:
            # Merge with the previous segment (extend its duration).
            prev = out[-1]
            out[-1] = BusRun(
                t_us=prev.t_us,
                value=prev.value,
                dur_us=round(seg_end - prev.t_us, round_ndigits),
            )
        else:
            out.append(
                BusRun(
                    t_us=round(seg_start, round_ndigits),
                    value=value,
                    dur_us=round(seg_end - seg_start, round_ndigits),
                )
            )
    return out
