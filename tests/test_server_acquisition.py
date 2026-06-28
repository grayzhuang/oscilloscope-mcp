"""Server tool tests for scope_channel / scope_timebase against a stub
scope (no hardware). Mirrors tests/test_server.py's scope_trigger style.
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp import server as server_mod
from oscilloscope_mcp.helpers.acquisition import AcquisitionValidationError
from oscilloscope_mcp.instruments._base import (
    ChannelState,
    TimebaseState,
)


_PROFILE = {
    "model": "RIGOL_DS1104Z",
    "capability": {
        "analog_bw_hz": 100e6,
        "channels": 4,
        "sample_rate_mode_dependent": {"1": 1e9, "2": 500e6, "4": 250e6},
        "memory_depth_pts_total": 24e6,
        "acquisition": {
            "channel": {
                "coupling_modes": ["AC", "DC", "GND"],
                "probe_ratios": [0.1, 1, 10, 100],
                "bw_limit_values": ["20M", "OFF"],
                "units": ["VOLTage", "WATT", "AMPere", "UNKNown"],
                "scale_v_per_div": {"min": 1e-3, "max": 100.0},
                "offset_v": {"min": -100.0, "max": 100.0},
            },
            "timebase": {
                "modes": ["MAIN", "XY", "ROLL"],
                "scale_s_per_div": {"min": 5e-9, "max": 50.0},
                "offset_s": {"min": -100.0, "max": 100.0},
            },
        },
    },
}


class _StubScope:
    """Records the setup it was asked to apply; returns a fixed state.
    Optionally raises on set (to test validation propagation)."""

    def __init__(
        self,
        channel_state: ChannelState,
        timebase_state: TimebaseState,
        raise_on_set: Exception | None = None,
        active_channels: int = 1,
    ) -> None:
        self.profile = _PROFILE
        self._ch = channel_state
        self._tb = timebase_state
        self._raise = raise_on_set
        self._active = active_channels
        self.last_channel_setup = None
        self.last_timebase_setup = None
        self.got_channel = None

    def active_channel_count(self) -> int:
        return self._active

    def get_channel(self, channel: int) -> ChannelState:
        self.got_channel = channel
        return self._ch

    def set_channel(self, setup) -> ChannelState:
        self.last_channel_setup = setup
        if self._raise is not None:
            raise self._raise
        return self._ch

    def get_timebase(self) -> TimebaseState:
        return self._tb

    def set_timebase(self, setup) -> TimebaseState:
        self.last_timebase_setup = setup
        if self._raise is not None:
            raise self._raise
        return self._tb


def _ch_state(bw_limit: str = "OFF") -> ChannelState:
    return ChannelState(
        channel=1, scale_v_per_div=0.5, offset_v=-1.0, coupling="DC",
        display=True, probe=10.0, bw_limit=bw_limit, invert=False, units="VOLT",
    )


def _tb_state(s_per_div: float = 1e-6) -> TimebaseState:
    return TimebaseState(s_per_div=s_per_div, offset_s=0.0, mode="MAIN")


def _tool(monkeypatch, scope, name):
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: scope)
    srv = server_mod.build_server()
    tools = srv._tool_manager.list_tools()
    for t in tools:
        if t.name == name:
            return t.fn
    raise AssertionError(f"tool {name!r} not registered")


# --- registration ---------------------------------------------------------


def test_acquisition_tools_registered() -> None:
    srv = server_mod.build_server()
    names = {t.name for t in srv._tool_manager.list_tools()}
    assert {"scope_channel", "scope_timebase"} <= names


# --- scope_channel --------------------------------------------------------


def test_scope_channel_read_only_when_channel_only(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubScope(_ch_state(), _tb_state())
    fn = _tool(monkeypatch, scope, "scope_channel")
    result = fn(channel=1)
    assert result["was_set"] is False
    assert scope.last_channel_setup is None   # read path never calls set_channel
    assert scope.got_channel == 1
    assert result["coupling"] == "DC"
    assert result["scale_v_per_div"] == 0.5
    assert result["caveats"] == []


def test_scope_channel_sets_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubScope(_ch_state(), _tb_state())
    fn = _tool(monkeypatch, scope, "scope_channel")
    result = fn(channel=1, scale_v_per_div=0.5, coupling="DC", probe=10)
    assert result["was_set"] is True
    assert scope.last_channel_setup.channel == 1
    assert scope.last_channel_setup.scale_v_per_div == 0.5
    assert scope.last_channel_setup.coupling == "DC"
    assert scope.last_channel_setup.probe == 10
    assert result["caveats"] == []           # bw_limit OFF → no caveat


def test_scope_channel_bw_limit_emits_caveat(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubScope(_ch_state(bw_limit="20M"), _tb_state())
    fn = _tool(monkeypatch, scope, "scope_channel")
    result = fn(channel=1, bw_limit="20M")
    assert result["bw_limit"] == "20M"
    assert any("bandwidth limit" in c.lower() for c in result["caveats"])


def test_scope_channel_validation_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubScope(
        _ch_state(), _tb_state(),
        raise_on_set=AcquisitionValidationError("coupling 'X' invalid"),
    )
    fn = _tool(monkeypatch, scope, "scope_channel")
    with pytest.raises(ValueError, match="invalid"):
        fn(channel=1, coupling="X")


# --- scope_timebase -------------------------------------------------------


def test_scope_timebase_read_only_when_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubScope(_ch_state(), _tb_state())
    fn = _tool(monkeypatch, scope, "scope_timebase")
    result = fn()
    assert result["was_set"] is False
    assert scope.last_timebase_setup is None
    assert result["mode"] == "MAIN"
    assert result["s_per_div"] == 1e-6
    assert result["caveats"] == []           # short timebase, 1ch → no caveat


def test_scope_timebase_sets_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubScope(_ch_state(), _tb_state(s_per_div=1e-3))
    fn = _tool(monkeypatch, scope, "scope_timebase")
    result = fn(s_per_div=1e-3, mode="MAIN")
    assert result["was_set"] is True
    assert scope.last_timebase_setup.s_per_div == 1e-3
    assert scope.last_timebase_setup.mode == "MAIN"


def test_scope_timebase_memory_downgrade_emits_caveat(monkeypatch: pytest.MonkeyPatch) -> None:
    # 10 ms/div × 12 div = 120 ms window; × 250 MSa/s (4-ch) = 30 Mpts;
    # per-channel memory = 24M/4 = 6 Mpts → downgrade caveat.
    scope = _StubScope(_ch_state(), _tb_state(s_per_div=10e-3), active_channels=4)
    fn = _tool(monkeypatch, scope, "scope_timebase")
    result = fn(s_per_div=10e-3)
    assert any("memory" in c.lower() and "downgraded" in c.lower()
               for c in result["caveats"])


def test_scope_timebase_validation_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubScope(
        _ch_state(), _tb_state(),
        raise_on_set=AcquisitionValidationError("timebase mode 'ZOOM' invalid"),
    )
    fn = _tool(monkeypatch, scope, "scope_timebase")
    with pytest.raises(ValueError, match="invalid"):
        fn(mode="ZOOM")
