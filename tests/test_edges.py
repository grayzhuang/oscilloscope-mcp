"""Unit tests for the edges helper — runs → RISE/FALL transitions."""

from __future__ import annotations

from oscilloscope_mcp.helpers.edges import FALL, RISE, Edge, edges_from_runs
from oscilloscope_mcp.helpers.rle import Run


def test_no_edges_for_single_run() -> None:
    assert edges_from_runs([Run(0.0, 0, 10.0)], "CHAN1") == []


def test_empty_runs() -> None:
    assert edges_from_runs([], "CHAN1") == []


def test_rise_then_fall() -> None:
    runs = [
        Run(t_us=0.0, level=0, dur_us=3.0),
        Run(t_us=3.0, level=1, dur_us=2.0),
        Run(t_us=5.0, level=0, dur_us=2.0),
    ]
    edges = edges_from_runs(runs, "CHAN1")
    assert edges == [
        Edge(t_us=3.0, channel="CHAN1", kind=RISE),
        Edge(t_us=5.0, channel="CHAN1", kind=FALL),
    ]


def test_edge_time_is_start_of_second_run() -> None:
    runs = [Run(-2.0, 1, 2.0), Run(0.0, 0, 5.0)]
    edges = edges_from_runs(runs, "CHAN2")
    assert len(edges) == 1
    assert edges[0].t_us == 0.0
    assert edges[0].kind == FALL
    assert edges[0].channel == "CHAN2"


def test_starting_high_first_transition_is_fall() -> None:
    runs = [Run(0.0, 1, 1.0), Run(1.0, 0, 1.0), Run(2.0, 1, 1.0)]
    kinds = [e.kind for e in edges_from_runs(runs, "CHAN1")]
    assert kinds == [FALL, RISE]


def test_channel_label_attached_to_every_edge() -> None:
    runs = [Run(0.0, 0, 1.0), Run(1.0, 1, 1.0), Run(2.0, 0, 1.0)]
    edges = edges_from_runs(runs, "CHAN3")
    assert all(e.channel == "CHAN3" for e in edges)
