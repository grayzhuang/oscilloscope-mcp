"""Unit tests for the RIGOL DS1000Z driver's acquisition setup paths —
channel (vertical) + timebase (horizontal) get/set against a fake
transport.

Exercises SCPI command shape, write_many ordering (probe / mode first),
and read-back parsing, mirroring tests/test_rigol_ds1000z_driver.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from oscilloscope_mcp.helpers.acquisition import AcquisitionValidationError
from oscilloscope_mcp.instruments import _load_profile
from oscilloscope_mcp.instruments._base import ChannelSetup, TimebaseSetup
from oscilloscope_mcp.instruments.rigol_ds1000z import RigolDs1000z


class _RecordingTransport:
    """In-memory SCPI transport: records writes, serves a query table.

    write_many records each command in order so tests can assert burst
    ordering (mirrors the real transport's single-connection batch).
    """

    def __init__(self, response_map: dict[str, str] | None = None) -> None:
        self.writes: list[str] = []
        self.queries: list[str] = []
        self._responses = response_map or {}

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)

    def write_many(self, cmds: list[str]) -> None:
        self.writes.extend(cmds)

    def query(self, cmd: str) -> str:
        self.queries.append(cmd)
        if cmd in self._responses:
            return self._responses[cmd]
        if cmd == "*IDN?":
            return "RIGOL TECHNOLOGIES,DS1104Z,SN,FW"
        if cmd == ":TIM:MAIN:SCAL?":
            return "1.000000e-06"
        if cmd == ":TIM:MAIN:OFFS?":
            return "0.000000e+00"
        if cmd == ":TIM:MODE?":
            return "MAIN"
        if cmd.endswith(":SCAL?"):
            return "1.000000e+00"
        if cmd.endswith(":OFFS?"):
            return "0.000000e+00"
        if cmd.endswith(":COUP?"):
            return "DC"
        if cmd.endswith(":DISP?"):
            return "1"
        if cmd.endswith(":PROB?"):
            return "1.000000e+01"
        if cmd.endswith(":BWL?"):
            return "OFF"
        if cmd.endswith(":INV?"):
            return "0"
        if cmd.endswith(":UNIT?"):
            return "VOLT"
        return ""


_PROFILE = _load_profile("rigol_ds1104z.yaml")


def _make_driver(**kw: Any) -> tuple[RigolDs1000z, _RecordingTransport]:
    t = _RecordingTransport(**kw)
    drv = RigolDs1000z(transport=t, profile=_PROFILE)
    drv.run_status_settle_s = 0
    return drv, t


# --- channel get ----------------------------------------------------------


def test_get_channel_reads_all_fields() -> None:
    drv, t = _make_driver(response_map={
        ":CHAN2:SCAL?": "5.000000e-01",
        ":CHAN2:OFFS?": "-1.000000e+00",
        ":CHAN2:COUP?": "AC",
        ":CHAN2:DISP?": "1",
        ":CHAN2:PROB?": "1.000000e+01",
        ":CHAN2:BWL?": "20M",
        ":CHAN2:INV?": "0",
        ":CHAN2:UNIT?": "VOLT",
    })
    state = drv.get_channel(2)
    assert state.channel == 2
    assert state.scale_v_per_div == pytest.approx(0.5)
    assert state.offset_v == pytest.approx(-1.0)
    assert state.coupling == "AC"
    assert state.display is True
    assert state.probe == pytest.approx(10.0)
    assert state.bw_limit == "20M"
    assert state.invert is False
    assert state.units == "VOLT"


def test_get_channel_validates_channel_number() -> None:
    drv, _ = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="out of range"):
        drv.get_channel(9)


# --- channel set ----------------------------------------------------------


def test_set_channel_full_sequence_probe_first() -> None:
    drv, t = _make_driver()
    drv.set_channel(ChannelSetup(
        channel=1,
        scale_v_per_div=0.5,
        offset_v=-1.0,
        coupling="ac",
        display=True,
        probe=10,
        bw_limit="20M",
        invert=False,
        units="volt",
    ))
    # Probe must precede scale/offset (it re-ranges the front end).
    assert t.writes.index(":CHAN1:PROB 10") < t.writes.index(":CHAN1:SCAL 0.5")
    assert t.writes.index(":CHAN1:PROB 10") < t.writes.index(":CHAN1:OFFS -1")
    assert ":CHAN1:COUP AC" in t.writes
    assert ":CHAN1:UNIT VOLTage" in t.writes
    assert ":CHAN1:SCAL 0.5" in t.writes
    assert ":CHAN1:OFFS -1" in t.writes
    assert ":CHAN1:BWL 20M" in t.writes
    assert ":CHAN1:INV OFF" in t.writes
    assert ":CHAN1:DISP ON" in t.writes


def test_set_channel_partial_only_writes_requested() -> None:
    drv, t = _make_driver()
    drv.set_channel(ChannelSetup(channel=3, coupling="DC"))
    assert t.writes == [":CHAN3:COUP DC"]


def test_set_channel_accepts_chan_string_number() -> None:
    drv, t = _make_driver()
    drv.set_channel(ChannelSetup(channel="CHAN4", display=False))
    assert ":CHAN4:DISP OFF" in t.writes


def test_set_channel_rejects_bad_coupling_before_any_write() -> None:
    drv, t = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="valid:"):
        drv.set_channel(ChannelSetup(channel=1, coupling="LFReject"))
    assert t.writes == []


def test_set_channel_rejects_unlisted_probe_before_any_write() -> None:
    drv, t = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="invalid"):
        drv.set_channel(ChannelSetup(channel=1, probe=3))
    assert t.writes == []


def test_set_channel_rejects_out_of_range_scale_before_any_write() -> None:
    drv, t = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="above maximum"):
        drv.set_channel(ChannelSetup(channel=1, scale_v_per_div=1e6))
    assert t.writes == []


def test_set_channel_rejects_bad_channel_before_any_write() -> None:
    drv, t = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="out of range"):
        drv.set_channel(ChannelSetup(channel=0, coupling="DC"))
    assert t.writes == []


# --- timebase get ---------------------------------------------------------


def test_get_timebase_reads_fields() -> None:
    drv, _ = _make_driver(response_map={
        ":TIM:MAIN:SCAL?": "2.000000e-03",
        ":TIM:MAIN:OFFS?": "1.000000e-04",
        ":TIM:MODE?": "MAIN",
    })
    state = drv.get_timebase()
    assert state.s_per_div == pytest.approx(2e-3)
    assert state.offset_s == pytest.approx(1e-4)
    assert state.mode == "MAIN"


# --- timebase set ---------------------------------------------------------


def test_set_timebase_full_sequence_mode_first() -> None:
    drv, t = _make_driver()
    drv.set_timebase(TimebaseSetup(s_per_div=1e-3, offset_s=2e-4, mode="main"))
    # Mode must land before scale/offset.
    assert t.writes.index(":TIM:MODE MAIN") < t.writes.index(":TIM:MAIN:SCAL 0.001")
    assert ":TIM:MAIN:SCAL 0.001" in t.writes
    assert ":TIM:MAIN:OFFS 0.0002" in t.writes


def test_set_timebase_partial_only_writes_requested() -> None:
    drv, t = _make_driver()
    drv.set_timebase(TimebaseSetup(s_per_div=5e-9))
    assert t.writes == [":TIM:MAIN:SCAL 5e-09"]


def test_set_timebase_rejects_below_min_before_any_write() -> None:
    drv, t = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="below minimum"):
        drv.set_timebase(TimebaseSetup(s_per_div=1e-9))
    assert t.writes == []


def test_set_timebase_rejects_bad_mode_before_any_write() -> None:
    drv, t = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="valid:"):
        drv.set_timebase(TimebaseSetup(mode="ZOOM"))
    assert t.writes == []
