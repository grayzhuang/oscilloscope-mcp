"""Derive an edge list from run-length-encoded runs.

An ``edge`` is the transition *between* two consecutive runs. Because a
run is one constant level, every boundary between runs is exactly one
edge whose direction is implied by the level it transitions *into*:

- into level 1 → ``"RISE"``
- into level 0 → ``"FALL"``

Each edge is an ``Edge(t_us, channel, kind)`` named tuple, where ``t_us``
is the start time of the *second* run (the instant the transition
happens) and ``channel`` is the source-channel label (e.g. ``"CHAN1"``)
so a multi-channel consumer can tell edges apart. The first run has no
preceding run, so it produces no edge.
"""

from __future__ import annotations

from typing import NamedTuple, Sequence

from oscilloscope_mcp.helpers.rle import Run


RISE = "RISE"
FALL = "FALL"


class Edge(NamedTuple):
    """One transition between two consecutive runs."""

    t_us: float
    channel: str
    kind: str  # "RISE" | "FALL"


def edges_from_runs(runs: Sequence[Run], channel: str) -> list[Edge]:
    """Return the list of :class:`Edge` transitions implied by ``runs``.

    Parameters
    ----------
    runs : sequence of Run
        Runs in time order, as produced by
        :func:`oscilloscope_mcp.helpers.rle.runs_from_levels`.
    channel : str
        Source-channel label attached to every edge (e.g. ``"CHAN1"``).

    Returns
    -------
    list[Edge]
        One edge per run boundary; empty if there are fewer than two runs.
        Direction is taken from the level the transition enters.
    """
    edges: list[Edge] = []
    for prev, cur in zip(runs, runs[1:]):
        if cur.level == prev.level:
            # Adjacent runs should differ in level by construction; skip a
            # degenerate equal-level boundary rather than emit a phantom edge.
            continue
        kind = RISE if cur.level == 1 else FALL
        edges.append(Edge(t_us=cur.t_us, channel=channel, kind=kind))
    return edges
