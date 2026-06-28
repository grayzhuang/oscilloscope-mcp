"""Unit tests for the bus helper — multi-channel runs → bus value runs."""

from __future__ import annotations

from oscilloscope_mcp.helpers.bus import BusRun, bus_runs_from_channels
from oscilloscope_mcp.helpers.rle import Run


def test_empty_channels() -> None:
    assert bus_runs_from_channels([]) == []
    assert bus_runs_from_channels([[], []]) == []


def test_two_bit_bus_value_packing_msb_first() -> None:
    # MSB (A) and LSB (B) over a window 0..4 µs:
    #   A: low 0..2, high 2..4
    #   B: high 0..1, low 1..3, high 3..4
    a = [Run(0.0, 0, 2.0), Run(2.0, 1, 2.0)]
    b = [Run(0.0, 1, 1.0), Run(1.0, 0, 2.0), Run(3.0, 1, 1.0)]
    bus = bus_runs_from_channels([a, b])  # A is MSB

    # Segment boundaries at 0,1,2,3 (ends 4):
    #   [0,1): A=0 B=1 -> 0b01 = 1
    #   [1,2): A=0 B=0 -> 0b00 = 0
    #   [2,3): A=1 B=0 -> 0b10 = 2
    #   [3,4): A=1 B=1 -> 0b11 = 3
    assert bus == [
        BusRun(t_us=0.0, value=1, dur_us=1.0),
        BusRun(t_us=1.0, value=0, dur_us=1.0),
        BusRun(t_us=2.0, value=2, dur_us=1.0),
        BusRun(t_us=3.0, value=3, dur_us=1.0),
    ]


def test_adjacent_equal_values_merge() -> None:
    # Two channels that never change after t=0 → one merged bus run.
    a = [Run(0.0, 1, 4.0)]
    b = [Run(0.0, 0, 2.0), Run(2.0, 0, 2.0)]  # B has a boundary but no change
    bus = bus_runs_from_channels([a, b])
    assert bus == [BusRun(t_us=0.0, value=2, dur_us=4.0)]


def test_single_channel_bus_equals_its_levels() -> None:
    a = [Run(0.0, 0, 2.0), Run(2.0, 1, 2.0)]
    bus = bus_runs_from_channels([a])
    assert bus == [
        BusRun(t_us=0.0, value=0, dur_us=2.0),
        BusRun(t_us=2.0, value=1, dur_us=2.0),
    ]
