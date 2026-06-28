"""Run-length encode a digital level stream into time-stamped runs.

A ``run`` is one contiguous stretch of constant digital level. The RLE
representation is what makes the event stream tiny: a 1200-point screen
waveform that is a clean 1 kHz square wave collapses from 1200 samples
to a handful of runs, which is the whole point of P1.5 (response well
under the 10 KB DoD budget).

Each run is a ``Run(t_us, level, dur_us)`` named tuple:

- ``t_us``   — start time of the run, microseconds (same origin as the
  sample time base; the trigger point is t = 0 on the DS1000Z).
- ``level``  — 0 or 1.
- ``dur_us`` — duration of the run in microseconds.

The time base is supplied as ``t0_us`` (time of sample index 0) and
``dt_us`` (per-sample interval). The driver computes these from the
:WAV:PREamble? fields (``xorigin``, ``xincrement``, ``xreference``) and
converts seconds → microseconds before calling here, keeping this module
pure and unit-agnostic.
"""

from __future__ import annotations

from typing import NamedTuple, Sequence


class Run(NamedTuple):
    """One run-length-encoded constant-level segment."""

    t_us: float
    level: int
    dur_us: float


def runs_from_levels(
    levels: Sequence[int],
    t0_us: float,
    dt_us: float,
    round_ndigits: int = 4,
) -> list[Run]:
    """Run-length-encode ``levels`` into a list of :class:`Run`.

    Parameters
    ----------
    levels : sequence of int
        Digital levels (0/1), one per sample, as produced by
        :func:`oscilloscope_mcp.helpers.quantize.quantize`.
    t0_us : float
        Time of sample index 0, in microseconds.
    dt_us : float
        Per-sample interval, in microseconds (> 0).
    round_ndigits : int
        Decimal places to round ``t_us`` / ``dur_us`` to, keeping the
        JSON compact and free of float noise.

    Returns
    -------
    list[Run]
        One entry per contiguous constant-level segment, in time order.
        Each run's ``dur_us`` spans from its first sample to (and
        including) the dwell of its last sample — i.e. ``count * dt_us``
        — so consecutive run start times tile without gaps.
    """
    if dt_us <= 0:
        raise ValueError(f"dt_us must be positive, got {dt_us}")
    if not levels:
        return []

    runs: list[Run] = []
    start_idx = 0
    cur = levels[0]
    n = len(levels)
    for i in range(1, n):
        if levels[i] != cur:
            runs.append(_make_run(cur, start_idx, i, t0_us, dt_us, round_ndigits))
            start_idx = i
            cur = levels[i]
    # Final run runs to the end of the buffer.
    runs.append(_make_run(cur, start_idx, n, t0_us, dt_us, round_ndigits))
    return runs


def _make_run(
    level: int,
    start_idx: int,
    end_idx: int,
    t0_us: float,
    dt_us: float,
    round_ndigits: int,
) -> Run:
    t_us = t0_us + start_idx * dt_us
    dur_us = (end_idx - start_idx) * dt_us
    return Run(
        t_us=round(t_us, round_ndigits),
        level=int(level),
        dur_us=round(dur_us, round_ndigits),
    )
