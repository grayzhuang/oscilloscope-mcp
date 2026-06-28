"""Smoke tests for the FastMCP server itself.

We don't drive the stdio JSON-RPC loop here; that's an integration
concern best left to a real MCP client. What we do check:

- :func:`build_server` returns a usable FastMCP instance.
- Both tools are registered with the expected names.
- The tool functions, when invoked directly (bypassing JSON-RPC),
  return well-shaped dicts and surface caveats correctly.

The scope itself is patched out — these tests do not require hardware.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oscilloscope_mcp import server as server_mod
from oscilloscope_mcp.helpers.measure import MeasureValidationError
from oscilloscope_mcp.helpers.trigger import TriggerValidationError
from oscilloscope_mcp.instruments import _load_profile
from oscilloscope_mcp.instruments._base import (
    CursorPair,
    ScreenshotPlan,
    ScreenshotResult,
    TriggerState,
    Waveform,
)


class _StubScope:
    def __init__(self, query_response: str = "STOP",
                 image_bytes: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
                 echo_cursors: bool = False) -> None:
        self._query = query_response
        self._image = image_bytes
        self._echo = echo_cursors
        self.queries: list[str] = []

    def query_raw(self, scpi: str) -> str:
        self.queries.append(scpi)
        return self._query

    def screenshot(self, plan: ScreenshotPlan) -> ScreenshotResult:
        cursors = plan.cursor_pairs if self._echo else []
        return ScreenshotResult(
            image_bytes=self._image,
            image_format=plan.image_format,
            cursors_set=cursors,
            channel_labels_applied=dict(plan.channel_labels),
        )


_TRIG_PROFILE = {
    "model": "RIGOL_DS1104Z",
    "capability": {
        "channels": 4,
        "trigger": {
            "sweep_modes": ["AUTO", "NORMAL", "SINGLE"],
            "coupling_modes": ["AC", "DC", "LFReject", "HFReject"],
            "status_values": ["TD", "WAIT", "RUN", "AUTO", "STOP"],
            "run_control": {"RUN": ":RUN", "STOP": ":STOP", "SINGLE": ":SINGle", "FORCE": ":TFORce"},
            "types": [
                {"keyword": "EDGE", "query": "EDGE", "subsystem": ":TRIGger:EDGe", "option": False,
                 "params": {
                     "source": {"scpi": "SOURce", "kind": "source"},
                     "slope": {"scpi": "SLOPe", "kind": "enum", "values": ["POSitive", "NEGative", "RFALl"]},
                     "level_v": {"scpi": "LEVel", "kind": "real", "unit": "V"},
                 }},
                {"keyword": "RUNT", "query": "RUNT", "subsystem": ":TRIGger:RUNT", "option": True},
            ],
        },
    },
}


class _StubTriggerScope:
    """Stub that records the TriggerSetup it was asked to apply and
    returns a fixed TriggerState (optionally raising on set).
    """

    def __init__(self, state: TriggerState, raise_on_set: Exception | None = None) -> None:
        self.profile = _TRIG_PROFILE
        self._state = state
        self._raise = raise_on_set
        self.last_setup = None
        self.last_action = None

    def get_trigger(self) -> TriggerState:
        return self._state

    def set_trigger(self, setup) -> TriggerState:
        self.last_setup = setup
        if self._raise is not None:
            raise self._raise
        return self._state

    def run_control(self, action: str) -> str:
        self.last_action = action
        return action.upper()


def _edge_state(mode: str = "EDGE") -> TriggerState:
    return TriggerState(
        mode=mode, status="STOP", sweep="AUTO", coupling="DC",
        holdoff_s=8e-9, params={"source": "CHAN1", "slope": "POS", "level_v": 1.5},
    )


def _registered_tool_callable(srv, name: str):
    """FastMCP stores registered tools internally; pull the underlying
    callable so we can drive it from tests."""
    tools = srv._tool_manager.list_tools()
    for t in tools:
        if t.name == name:
            return t.fn
    raise AssertionError(f"tool {name!r} not registered on server")


def test_build_server_returns_named_instance() -> None:
    srv = server_mod.build_server()
    assert srv.name == server_mod.SERVER_NAME


def test_both_tools_are_registered() -> None:
    srv = server_mod.build_server()
    names = {t.name for t in srv._tool_manager.list_tools()}
    assert {
        "scope_query", "scope_screenshot", "scope_trigger", "scope_measure",
        "scope_measure_stat", "scope_channel", "scope_timebase",
        "scope_waveform", "scope_acquire", "scope_capture", "scope_compare",
    } <= names


def test_scope_query_tool_returns_response_and_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubScope(query_response="STOP")
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: stub)
    srv = server_mod.build_server()
    fn = _registered_tool_callable(srv, "scope_query")

    result = fn(scpi=":TRIG:STAT?")
    assert result["response"] == "STOP"
    assert result["is_query"] is True
    assert result["caveats"], "raw-SCPI caveat must always be present"
    assert stub.queries == [":TRIG:STAT?"]


def test_scope_query_tool_rejects_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: _StubScope())
    srv = server_mod.build_server()
    fn = _registered_tool_callable(srv, "scope_query")
    with pytest.raises(ValueError, match="scpi"):
        fn(scpi="  ")


def test_scope_screenshot_tool_writes_png_and_returns_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubScope()
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: stub)
    srv = server_mod.build_server()
    fn = _registered_tool_callable(srv, "scope_screenshot")

    out = tmp_path / "shot.png"
    result = fn(
        output_path=str(out),
        channel_labels={"1": "DIR", "2": "STP"},
    )
    assert Path(result["screenshot_path"]) == out
    assert out.read_bytes().startswith(b"\x89PNG")
    assert result["bytes_written"] > 0
    assert result["channel_labels_applied"] == {"1": "DIR", "2": "STP"}


def test_scope_screenshot_tool_cursor_caveat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubScope(echo_cursors=True)
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: stub)
    srv = server_mod.build_server()
    fn = _registered_tool_callable(srv, "scope_screenshot")
    out = tmp_path / "shot.png"

    result = fn(
        output_path=str(out),
        cursors=[
            {"label": "span", "source_channel": 1,
             "ax_t_s": 0.0, "bx_t_s": 2e-6},
        ],
    )
    assert len(result["cursors_set"]) == 1
    assert result["cursors_set"][0]["delta_t_s"] == pytest.approx(2e-6)
    assert result["caveats"], "cursor caveat must be present when cursors set"


def test_scope_screenshot_rejects_directory_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: _StubScope())
    srv = server_mod.build_server()
    fn = _registered_tool_callable(srv, "scope_screenshot")
    with pytest.raises(ValueError, match="must be a file path"):
        fn(output_path=str(tmp_path))


# --- scope_trigger --------------------------------------------------------


def _trigger_tool(monkeypatch, scope):
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: scope)
    srv = server_mod.build_server()
    return _registered_tool_callable(srv, "scope_trigger")


def test_scope_trigger_read_only_when_no_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubTriggerScope(_edge_state())
    fn = _trigger_tool(monkeypatch, scope)
    result = fn()
    assert result["was_set"] is False
    assert scope.last_setup is None        # read path never calls set_trigger
    assert result["mode"] == "EDGE"
    assert result["params"]["level_v"] == 1.5
    # Read response advertises what's settable for the current mode.
    assert "slope" in result["accepted_params"]
    assert result["accepted_params"]["slope"]["kind"] == "enum"
    assert result["caveats"] == []


def test_scope_trigger_sets_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubTriggerScope(_edge_state())
    fn = _trigger_tool(monkeypatch, scope)
    result = fn(mode="EDGE", params={"source": "1", "slope": "rising", "level_v": 1.5})
    assert result["was_set"] is True
    assert scope.last_setup.mode == "EDGE"
    assert scope.last_setup.params == {"source": "1", "slope": "rising", "level_v": 1.5}
    assert result["caveats"] == []         # EDGE is a standard type


def test_scope_trigger_option_type_not_applied_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unit lacks the RUNT license → readback still reports EDGE.
    scope = _StubTriggerScope(_edge_state(mode="EDGE"))
    fn = _trigger_tool(monkeypatch, scope)
    result = fn(mode="RUNT")
    # Two caveats: the option-license warning + the did-not-apply mismatch.
    assert len(result["caveats"]) == 2
    joined = " ".join(result["caveats"]).lower()
    assert "option" in joined
    assert "did not apply" in joined


def test_scope_trigger_validation_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubTriggerScope(
        _edge_state(),
        raise_on_set=TriggerValidationError("mode 'BOGUS' is not supported"),
    )
    fn = _trigger_tool(monkeypatch, scope)
    with pytest.raises(ValueError, match="not supported"):
        fn(mode="BOGUS")


def test_scope_trigger_run_control_action(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubTriggerScope(_edge_state())
    fn = _trigger_tool(monkeypatch, scope)
    result = fn(action="single")
    assert scope.last_action == "single"
    assert result["action_applied"] == "SINGLE"
    assert result["was_set"] is False  # action alone is not a config "set"


def test_scope_trigger_rejects_unknown_action(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubTriggerScope(_edge_state())
    fn = _trigger_tool(monkeypatch, scope)
    with pytest.raises(ValueError, match="run action"):
        fn(action="PAUSE")
    assert scope.last_action is None  # rejected before any control issued


# --- scope_measure --------------------------------------------------------


_MEASURE_PROFILE = _load_profile("rigol_ds1104z.yaml")


class _StubMeasureScope:
    """Stub scope for the scope_measure tool: records the measure() call
    and returns a fixed measurement map, plus the state-snapshot methods
    the tool reads for observation-limit caveats.
    """

    def __init__(
        self,
        measurements: dict[str, float | None],
        active_channels: int = 1,
        timebase_s_per_div: float = 1e-6,
        raise_on_measure: Exception | None = None,
    ) -> None:
        self.profile = _MEASURE_PROFILE
        self._measurements = measurements
        self._active = active_channels
        self._timebase = timebase_s_per_div
        self._raise = raise_on_measure
        self.last_call: tuple | None = None

    def measure(self, items, source="CHAN1", source2=None):
        self.last_call = (list(items), source, source2)
        if self._raise is not None:
            raise self._raise
        return dict(self._measurements)

    def active_channel_count(self) -> int:
        return self._active

    def timebase_s_per_div(self) -> float:
        return self._timebase


def _measure_tool(monkeypatch, scope):
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: scope)
    srv = server_mod.build_server()
    return _registered_tool_callable(srv, "scope_measure")


def test_scope_measure_returns_values_units_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _StubMeasureScope({"FREQUENCY": 1000.0, "VPP": 3.3})
    fn = _measure_tool(monkeypatch, scope)
    result = fn(items=["FREQUENCY", "VPP"], source="1")
    assert result["source"] == "CHANnel1"          # normalized echo
    assert result["measurements"] == {"FREQUENCY": 1000.0, "VPP": 3.3}
    assert result["units"] == {"FREQUENCY": "Hz", "VPP": "V"}
    assert scope.last_call == (["FREQUENCY", "VPP"], "1", None)
    assert result["caveats"] == []                 # 1ch, slow timebase → none


def test_scope_measure_unmeasurable_item_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _StubMeasureScope({"FREQUENCY": None, "VPP": 3.3})
    fn = _measure_tool(monkeypatch, scope)
    result = fn(items=["FREQUENCY", "VPP"])
    assert result["measurements"]["FREQUENCY"] is None
    joined = " ".join(result["caveats"]).lower()
    assert "un-measurable" in joined and "frequency" in joined


def test_scope_measure_attaches_observation_limit_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 4-channel mode forces the DS1000Z down to 250 MSa/s → sample-rate
    # downgrade caveat from compute_caveats().
    scope = _StubMeasureScope({"VPP": 3.3}, active_channels=4)
    fn = _measure_tool(monkeypatch, scope)
    result = fn(items=["VPP"])
    joined = " ".join(result["caveats"]).lower()
    assert "sample rate" in joined


def test_scope_measure_dual_source_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _StubMeasureScope({"RDELAY": 2.5e-8})
    fn = _measure_tool(monkeypatch, scope)
    result = fn(items=["RDELAY"], source="CHAN1", source2="CHAN2")
    assert result["source"] == "CHANnel1"
    assert result["source2"] == "CHANnel2"
    assert scope.last_call == (["RDELAY"], "CHAN1", "CHAN2")
    assert result["units"]["RDELAY"] == "s"


def test_scope_measure_rejects_unknown_item(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubMeasureScope({})
    fn = _measure_tool(monkeypatch, scope)
    with pytest.raises(ValueError, match="not supported"):
        fn(items=["NOPE"])


def test_scope_measure_rejects_empty_items(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _StubMeasureScope({})
    fn = _measure_tool(monkeypatch, scope)
    with pytest.raises(ValueError, match="at least one"):
        fn(items=[])


def test_scope_measure_validation_error_is_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MeasureValidationError is a ValueError subclass → clean tool error.
    scope = _StubMeasureScope({})
    fn = _measure_tool(monkeypatch, scope)
    with pytest.raises(ValueError):
        fn(items=["VPP"], source="99")
    assert issubclass(MeasureValidationError, ValueError)


# --- scope_measure_stat ---------------------------------------------------


from oscilloscope_mcp.instruments._base import MeasureStatResult


class _StubMeasureStatScope:
    """Stub scope for the scope_measure_stat tool."""

    def __init__(
        self,
        stat_results: list[MeasureStatResult],
        active_channels: int = 1,
        timebase_s_per_div: float = 1e-6,
    ) -> None:
        self.profile = _MEASURE_PROFILE
        self._stat_results = stat_results
        self._active = active_channels
        self._timebase = timebase_s_per_div
        self.writes: list[str] = []

    class _FakeTransport:
        def __init__(self, outer):
            self._outer = outer

        def write(self, cmd: str) -> None:
            self._outer.writes.append(cmd)

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)

    @property
    def transport(self):
        return self._FakeTransport(self)

    def measure_statistics(self, items, source="CHAN1", stat_types=None):
        return list(self._stat_results)

    def active_channel_count(self) -> int:
        return self._active

    def timebase_s_per_div(self) -> float:
        return self._timebase


def _measure_stat_tool(monkeypatch, scope):
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: scope)
    srv = server_mod.build_server()
    return _registered_tool_callable(srv, "scope_measure_stat")


def test_scope_measure_stat_returns_statistics_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stat_results = [
        MeasureStatResult(
            item="FREQUENCY", source="CHANnel1",
            current=1000.0, maximum=1001.0, minimum=999.0,
            average=1000.1, deviation=0.5, count=42,
        ),
    ]
    scope = _StubMeasureStatScope(stat_results)
    fn = _measure_stat_tool(monkeypatch, scope)
    result = fn(items=["FREQUENCY"], source="1")
    assert result["source"] == "CHANnel1"
    assert len(result["statistics"]) == 1
    stat = result["statistics"][0]
    assert stat["item"] == "FREQUENCY"
    assert stat["current"] == 1000.0
    assert stat["max"] == 1001.0
    assert stat["min"] == 999.0
    assert stat["avg"] == 1000.1
    assert stat["dev"] == 0.5
    assert stat["count"] == 42
    assert "caveats" in result


def test_scope_measure_stat_mode_writes_scpi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _StubMeasureStatScope([
        MeasureStatResult(item="VPP", source="CHANnel1", current=3.3),
    ])
    fn = _measure_stat_tool(monkeypatch, scope)
    result = fn(items=["VPP"], source="1", mode="extremum")
    assert ":MEAS:STAT:MODE EXTRemum" in scope.writes


def test_scope_measure_stat_reset_writes_scpi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _StubMeasureStatScope([
        MeasureStatResult(item="VPP", source="CHANnel1", current=3.3),
    ])
    fn = _measure_stat_tool(monkeypatch, scope)
    result = fn(items=["VPP"], source="1", reset=True)
    assert ":MEAS:STAT:RES" in scope.writes


def test_scope_measure_stat_rejects_unknown_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _StubMeasureStatScope([])
    fn = _measure_stat_tool(monkeypatch, scope)
    with pytest.raises(ValueError, match="not supported"):
        fn(items=["NOPE"])


def test_scope_measure_stat_rejects_unknown_stat_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _StubMeasureStatScope([])
    fn = _measure_stat_tool(monkeypatch, scope)
    with pytest.raises(ValueError, match="not supported"):
        fn(items=["VPP"], stat_types=["BOGUS"])


def test_scope_measure_stat_rejects_unknown_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _StubMeasureStatScope([])
    fn = _measure_stat_tool(monkeypatch, scope)
    with pytest.raises(ValueError, match="not supported"):
        fn(items=["VPP"], mode="BOGUS")


# --- scope_waveform -------------------------------------------------------


class _StubWaveformScope:
    """Stub returning a fixed Waveform, recording the requested source."""

    def __init__(self, waveform: Waveform) -> None:
        self._wf = waveform
        self.last_source: str | None = None
        self.last_mode: str | None = None

    def read_waveform(self, source: str, mode: str = "NORMal") -> Waveform:
        self.last_source = source
        self.last_mode = mode
        return self._wf


def _waveform_tool(monkeypatch, scope):
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: scope)
    srv = server_mod.build_server()
    return _registered_tool_callable(srv, "scope_waveform")


def test_scope_waveform_returns_runs_edges_and_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One clean high pulse: low,low,low,high,high,high,low,low at 1 µs/pt.
    wf = Waveform(
        source="CHAN1",
        volts=[0, 0, 0, 3, 3, 3, 0, 0],
        t0_s=0.0,
        dt_s=1e-6,
    )
    scope = _StubWaveformScope(wf)
    fn = _waveform_tool(monkeypatch, scope)

    result = fn(source="CHAN1", threshold_v=1.5, hysteresis_v=0.1)
    assert scope.last_source == "CHAN1"
    assert result["source"] == "CHAN1"
    assert result["n_samples"] == 8
    # runs are [t_us, level, dur_us] lists (JSON-friendly).
    assert result["runs"] == [
        [0.0, 0, 3.0],
        [3.0, 1, 3.0],
        [6.0, 0, 2.0],
    ]
    assert result["edges"] == [
        [3.0, "CHAN1", "RISE"],
        [6.0, "CHAN1", "FALL"],
    ]
    assert result["caveats"] == []  # 0.1 V hyst on 3 V p-p is < 20%


def test_scope_waveform_detection_miss_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wf = Waveform(source="CHAN1", volts=[0, 0, 3, 3], t0_s=0.0, dt_s=1e-6)
    scope = _StubWaveformScope(wf)
    fn = _waveform_tool(monkeypatch, scope)
    # 1.0 V hysteresis on a 3 V p-p signal = 33% > 20% → detection-miss.
    result = fn(source="CHAN1", threshold_v=1.5, hysteresis_v=1.0)
    assert any("hysteresis" in c.lower() for c in result["caveats"])


def test_scope_waveform_response_under_10kb_for_square_wave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD: a 1200-pt clean square wave produces a runs+edges response
    well under 10 KB."""
    import json

    volts: list[float] = []
    for _ in range(6):
        volts += [0.0] * 100 + [3.0] * 100
    wf = Waveform(source="CHAN1", volts=volts, t0_s=0.0, dt_s=1e-6)
    scope = _StubWaveformScope(wf)
    fn = _waveform_tool(monkeypatch, scope)

    result = fn(source="CHAN1", threshold_v=1.5, hysteresis_v=0.1)
    assert result["n_samples"] == 1200
    assert len(result["runs"]) == 12  # collapses from 1200 samples
    assert len(json.dumps(result).encode("utf-8")) < 10_240


def test_scope_waveform_rejects_negative_hysteresis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wf = Waveform(source="CHAN1", volts=[0, 3], t0_s=0.0, dt_s=1e-6)
    fn = _waveform_tool(monkeypatch, _StubWaveformScope(wf))
    with pytest.raises(ValueError, match="hysteresis_v"):
        fn(source="CHAN1", hysteresis_v=-0.1)


def test_scope_waveform_truncates_and_caveats_when_too_many_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alternating every sample → 1 run per sample. Exceed _MAX_RUNS.
    n = server_mod._MAX_RUNS + 50
    volts = [3.0 if i % 2 else 0.0 for i in range(n)]
    wf = Waveform(source="CHAN1", volts=volts, t0_s=0.0, dt_s=1e-6)
    fn = _waveform_tool(monkeypatch, _StubWaveformScope(wf))
    result = fn(source="CHAN1", threshold_v=1.5, hysteresis_v=0.0)
    assert len(result["runs"]) == server_mod._MAX_RUNS
    assert any("truncat" in c.lower() for c in result["caveats"])


def test_scope_waveform_raw_mode_passes_mode_to_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope_waveform with mode='RAW' passes mode through to read_waveform."""
    wf = Waveform(
        source="CHAN1",
        volts=[0, 0, 3, 3, 0, 0],
        t0_s=0.0,
        dt_s=1e-9,
    )
    scope = _StubWaveformScope(wf)
    fn = _waveform_tool(monkeypatch, scope)
    result = fn(source="CHAN1", mode="RAW", threshold_v=1.5, hysteresis_v=0.1)
    assert scope.last_mode == "RAW"
    assert result["mode"] == "RAW"
    assert result["n_samples"] == 6
    # RAW mode emits a caveat noting the point count.
    assert any("RAW mode" in c for c in result["caveats"])


def test_scope_waveform_normal_mode_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope_waveform with mode='NORMal' passes the mode correctly."""
    wf = Waveform(source="CHAN1", volts=[0, 3], t0_s=0.0, dt_s=1e-6)
    scope = _StubWaveformScope(wf)
    fn = _waveform_tool(monkeypatch, scope)
    result = fn(source="CHAN1", mode="NORMal")
    assert scope.last_mode == "NORMal"
    assert result["mode"] == "NORMAL"


def test_scope_waveform_rejects_invalid_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope_waveform rejects an invalid mode string."""
    wf = Waveform(source="CHAN1", volts=[0, 3], t0_s=0.0, dt_s=1e-6)
    fn = _waveform_tool(monkeypatch, _StubWaveformScope(wf))
    with pytest.raises(ValueError, match="mode"):
        fn(source="CHAN1", mode="BOGUS")


# --- scope_acquire --------------------------------------------------------

from oscilloscope_mcp.instruments._base import AcquireState


class _StubAcquireScope:
    """Stub scope for the scope_acquire tool."""

    def __init__(
        self,
        state: AcquireState,
        raise_on_set: Exception | None = None,
    ) -> None:
        self.profile = _MEASURE_PROFILE
        self._state = state
        self._raise = raise_on_set
        self.last_setup = None

    def get_acquire(self) -> AcquireState:
        return self._state

    def set_acquire(self, setup):
        self.last_setup = setup
        if self._raise is not None:
            raise self._raise
        return self._state


def _acquire_tool(monkeypatch, scope):
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: scope)
    srv = server_mod.build_server()
    return _registered_tool_callable(srv, "scope_acquire")


def test_scope_acquire_read_only_when_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    state = AcquireState(type="NORM", averages=2, memory_depth="AUTO", sample_rate=1e9)
    scope = _StubAcquireScope(state)
    fn = _acquire_tool(monkeypatch, scope)
    result = fn()
    assert result["was_set"] is False
    assert scope.last_setup is None
    assert result["type"] == "NORM"
    assert result["averages"] == 2
    assert result["memory_depth"] == "AUTO"
    assert result["sample_rate"] == 1e9
    assert result["caveats"] == []


def test_scope_acquire_sets_type_and_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    state = AcquireState(type="NORM", averages=2, memory_depth=12000, sample_rate=5e8)
    scope = _StubAcquireScope(state)
    fn = _acquire_tool(monkeypatch, scope)
    result = fn(type="NORMal", averages=4, memory_depth=12000)
    assert result["was_set"] is True
    assert scope.last_setup is not None
    assert scope.last_setup.type == "NORMal"
    assert scope.last_setup.averages == 4
    assert scope.last_setup.memory_depth == 12000


def test_scope_acquire_averaging_caveat(monkeypatch: pytest.MonkeyPatch) -> None:
    state = AcquireState(type="AVER", averages=16, memory_depth="AUTO", sample_rate=1e9)
    scope = _StubAcquireScope(state)
    fn = _acquire_tool(monkeypatch, scope)
    result = fn(type="AVERages")
    joined = " ".join(result["caveats"]).lower()
    assert "averaging" in joined and "slows" in joined


def test_scope_acquire_no_caveat_for_normal_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    state = AcquireState(type="NORM", averages=2, memory_depth="AUTO", sample_rate=1e9)
    scope = _StubAcquireScope(state)
    fn = _acquire_tool(monkeypatch, scope)
    result = fn()
    assert result["caveats"] == []


# --- scope_capture --------------------------------------------------------

from oscilloscope_mcp.instruments._base import (
    AcquireSetup,
    AcquireState,
    ChannelSetup,
    ChannelState,
    TimebaseSetup,
    TimebaseState,
    TriggerSetup,
    TriggerState,
    Waveform,
    ScreenshotPlan,
    ScreenshotResult,
)


class _StubCaptureScope:
    """Comprehensive stub scope with all methods needed by scope_capture.

    Supports configurable trigger polling (how many polls before TD),
    multi-channel waveforms, measurements, and screenshots.
    """

    def __init__(
        self,
        *,
        trigger_polls_to_td: int = 1,
        channels_displayed: list[int] | None = None,
        waveform_volts: list[float] | None = None,
        measurements: dict[str, float | None] | None = None,
        image_bytes: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    ) -> None:
        self.profile = _MEASURE_PROFILE
        self._polls_to_td = trigger_polls_to_td
        self._poll_count = 0
        self._channels_displayed = channels_displayed or [1, 2]
        self._wf_volts = waveform_volts or [0, 0, 0, 3, 3, 3, 0, 0]
        self._measurements = measurements or {"FREQUENCY": 1000.0, "VPP": 3.3}
        self._image = image_bytes

        # Recording
        self.trigger_setup_calls: list[TriggerSetup] = []
        self.channel_setup_calls: list[ChannelSetup] = []
        self.timebase_setup_calls: list[TimebaseSetup] = []
        self.acquire_setup_calls: list[AcquireSetup] = []
        self.run_control_calls: list[str] = []
        self.get_trigger_calls: int = 0
        self.read_waveform_calls: list[tuple] = []
        self.measure_calls: list[tuple] = []
        self.screenshot_calls: list[ScreenshotPlan] = []

    # --- trigger ---

    def get_trigger(self) -> TriggerState:
        self.get_trigger_calls += 1
        self._poll_count += 1
        if self._poll_count >= self._polls_to_td:
            status = "TD"
        else:
            status = "WAIT"
        return TriggerState(
            mode="EDGE", status=status, sweep="SINGLE",
            coupling="DC", holdoff_s=8e-9,
            params={"source": "CHAN1", "slope": "POS"},
        )

    def set_trigger(self, setup: TriggerSetup) -> TriggerState:
        self.trigger_setup_calls.append(setup)
        return TriggerState(
            mode=setup.mode or "EDGE", status="WAIT", sweep="SINGLE",
            coupling="DC", holdoff_s=8e-9,
        )

    def run_control(self, action: str) -> str:
        self.run_control_calls.append(action)
        return action.upper()

    # --- channel ---

    def get_channel(self, channel: int) -> ChannelState:
        displayed = channel in self._channels_displayed
        return ChannelState(
            channel=channel,
            scale_v_per_div=1.0,
            offset_v=0.0,
            coupling="DC",
            display=displayed,
            probe=1.0,
            bw_limit="OFF",
            invert=False,
            units="VOLTage",
        )

    def set_channel(self, setup: ChannelSetup) -> ChannelState:
        self.channel_setup_calls.append(setup)
        return self.get_channel(setup.channel)

    # --- timebase ---

    def get_timebase(self) -> TimebaseState:
        return TimebaseState(s_per_div=1e-6, offset_s=0.0, mode="MAIN")

    def set_timebase(self, setup: TimebaseSetup) -> TimebaseState:
        self.timebase_setup_calls.append(setup)
        return TimebaseState(
            s_per_div=setup.s_per_div or 1e-6,
            offset_s=setup.offset_s or 0.0,
            mode=setup.mode or "MAIN",
        )

    # --- acquire ---

    def get_acquire(self) -> AcquireState:
        return AcquireState(type="NORM", averages=2, memory_depth="AUTO", sample_rate=1e9)

    def set_acquire(self, setup: AcquireSetup) -> AcquireState:
        self.acquire_setup_calls.append(setup)
        return AcquireState(
            type=setup.type or "NORM",
            averages=setup.averages or 2,
            memory_depth=setup.memory_depth or "AUTO",
            sample_rate=1e9,
        )

    # --- waveform ---

    def read_waveform(self, source: str, mode: str = "NORMal") -> Waveform:
        self.read_waveform_calls.append((source, mode))
        return Waveform(
            source=source,
            volts=list(self._wf_volts),
            t0_s=0.0,
            dt_s=1e-6,
        )

    # --- measurements ---

    def measure(self, items, source="CHAN1", source2=None):
        self.measure_calls.append((items, source, source2))
        return dict(self._measurements)

    # --- screenshot ---

    def screenshot(self, plan: ScreenshotPlan) -> ScreenshotResult:
        self.screenshot_calls.append(plan)
        return ScreenshotResult(
            image_bytes=self._image,
            image_format=plan.image_format,
            cursors_set=[],
            channel_labels_applied=dict(plan.channel_labels),
        )

    # --- state snapshot ---

    def active_channel_count(self) -> int:
        return len(self._channels_displayed)

    def timebase_s_per_div(self) -> float:
        return 1e-6


def _capture_tool(monkeypatch, scope):
    monkeypatch.setattr(server_mod, "open_scope", lambda **kw: scope)
    srv = server_mod.build_server()
    return _registered_tool_callable(srv, "scope_capture")


def test_scope_capture_single_triggers_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SINGLE mode arms, polls once to TD, reads waveform, returns triggered=True."""
    scope = _StubCaptureScope(trigger_polls_to_td=1, channels_displayed=[1])
    fn = _capture_tool(monkeypatch, scope)

    result = fn(
        channels=["CHAN1"],
        sweep="SINGLE",
        threshold_v=1.5,
        hysteresis_v=0.1,
        timeout_s=5.0,
    )

    assert result["triggered"] is True
    assert result["channels"] == ["CHAN1"]
    assert "CHAN1" in result["waveform"]
    assert result["waveform"]["CHAN1"]["n_samples"] == 8
    assert len(result["waveform"]["CHAN1"]["runs"]) > 0
    assert len(result["waveform"]["CHAN1"]["edges"]) > 0
    assert result["bus_runs"] is None  # single channel
    assert result["measurements"] is None  # no measurements requested
    assert result["screenshot_path"] is None
    assert "SINGLE" in scope.run_control_calls


def test_scope_capture_single_timeout_with_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SINGLE mode times out → triggered=False with a timeout caveat."""
    # Set polls_to_td very high so it times out (with very short timeout)
    scope = _StubCaptureScope(trigger_polls_to_td=9999, channels_displayed=[1])

    # Monkey-patch time.sleep so the test doesn't actually wait
    monkeypatch.setattr("time.sleep", lambda s: None)

    fn = _capture_tool(monkeypatch, scope)

    result = fn(
        channels=["CHAN1"],
        sweep="SINGLE",
        timeout_s=0.5,
    )

    assert result["triggered"] is False
    assert any("timeout" in c.lower() for c in result["caveats"])
    # Still reads waveform (best-effort)
    assert "CHAN1" in result["waveform"]


def test_scope_capture_auto_mode_reads_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTO sweep skips arm+poll and reads immediately."""
    scope = _StubCaptureScope(channels_displayed=[1])
    fn = _capture_tool(monkeypatch, scope)

    result = fn(
        channels=["CHAN1"],
        sweep="AUTO",
    )

    assert result["triggered"] is True
    assert scope.run_control_calls == []  # no SINGLE issued
    assert scope.get_trigger_calls == 0  # no polling
    assert "CHAN1" in result["waveform"]


def test_scope_capture_multi_channel_produces_bus_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-channel capture produces bus_runs alignment."""
    scope = _StubCaptureScope(
        trigger_polls_to_td=1,
        channels_displayed=[1, 2],
    )
    fn = _capture_tool(monkeypatch, scope)

    result = fn(
        channels=["CHAN1", "CHAN2"],
        sweep="SINGLE",
        timeout_s=5.0,
    )

    assert result["triggered"] is True
    assert set(result["channels"]) == {"CHAN1", "CHAN2"}
    assert "CHAN1" in result["waveform"]
    assert "CHAN2" in result["waveform"]
    assert result["bus_runs"] is not None
    assert len(result["bus_runs"]) > 0
    # Each bus run is [t_us, value, dur_us]
    assert len(result["bus_runs"][0]) == 3


def test_scope_capture_screenshot_produces_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """screenshot_path causes a PNG to be written."""
    scope = _StubCaptureScope(trigger_polls_to_td=1, channels_displayed=[1])
    fn = _capture_tool(monkeypatch, scope)
    out = tmp_path / "capture.png"

    result = fn(
        channels=["CHAN1"],
        sweep="SINGLE",
        screenshot_path=str(out),
        timeout_s=5.0,
    )

    assert result["screenshot_path"] == str(out)
    assert out.exists()
    assert out.read_bytes().startswith(b"\x89PNG")
    assert len(scope.screenshot_calls) == 1


def test_scope_capture_measurements_are_per_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measurements are collected per-channel when requested."""
    scope = _StubCaptureScope(
        trigger_polls_to_td=1,
        channels_displayed=[1, 2],
        measurements={"FREQUENCY": 50.0, "VPP": 3.3},
    )
    fn = _capture_tool(monkeypatch, scope)

    result = fn(
        channels=["CHAN1", "CHAN2"],
        sweep="SINGLE",
        measurements=["FREQUENCY", "VPP"],
        timeout_s=5.0,
    )

    assert result["measurements"] is not None
    assert "CHAN1" in result["measurements"]
    assert "CHAN2" in result["measurements"]
    assert result["measurements"]["CHAN1"]["FREQUENCY"] == 50.0
    assert result["measurements"]["CHAN2"]["VPP"] == 3.3
    # Two measure calls (one per channel)
    assert len(scope.measure_calls) == 2


def test_scope_capture_applies_trigger_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trigger mode and params are applied before arming."""
    scope = _StubCaptureScope(trigger_polls_to_td=1, channels_displayed=[1])
    fn = _capture_tool(monkeypatch, scope)

    result = fn(
        channels=["CHAN1"],
        trigger_mode="EDGE",
        trigger_params={"source": "CHAN1", "slope": "NEG"},
        sweep="SINGLE",
        timeout_s=5.0,
    )

    assert len(scope.trigger_setup_calls) == 1
    assert scope.trigger_setup_calls[0].mode == "EDGE"
    assert scope.trigger_setup_calls[0].params == {"source": "CHAN1", "slope": "NEG"}


def test_scope_capture_applies_timebase_and_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timebase and acquire settings are applied."""
    scope = _StubCaptureScope(trigger_polls_to_td=1, channels_displayed=[1])
    fn = _capture_tool(monkeypatch, scope)

    result = fn(
        channels=["CHAN1"],
        sweep="SINGLE",
        timebase_s_per_div=1e-3,
        timebase_offset_s=0.5e-3,
        acquire_type="NORMal",
        memory_depth="AUTO",
        timeout_s=5.0,
    )

    assert len(scope.timebase_setup_calls) == 1
    assert scope.timebase_setup_calls[0].s_per_div == 1e-3
    assert scope.timebase_setup_calls[0].offset_s == 0.5e-3
    assert len(scope.acquire_setup_calls) == 1
    assert scope.acquire_setup_calls[0].type == "NORMal"
    assert scope.acquire_setup_calls[0].memory_depth == "AUTO"


def test_scope_capture_applies_channel_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Channel settings are applied per-channel."""
    scope = _StubCaptureScope(trigger_polls_to_td=1, channels_displayed=[1, 2])
    fn = _capture_tool(monkeypatch, scope)

    result = fn(
        channels=["CHAN1", "CHAN2"],
        channel_settings={
            "1": {"scale_v_per_div": 0.5, "coupling": "DC"},
            "2": {"scale_v_per_div": 1.0, "offset_v": 2.0},
        },
        sweep="SINGLE",
        timeout_s=5.0,
    )

    assert len(scope.channel_setup_calls) == 2
    ch1 = scope.channel_setup_calls[0]
    assert ch1.channel == 1
    assert ch1.scale_v_per_div == 0.5
    assert ch1.coupling == "DC"
    ch2 = scope.channel_setup_calls[1]
    assert ch2.channel == 2
    assert ch2.scale_v_per_div == 1.0
    assert ch2.offset_v == 2.0


def test_scope_capture_auto_discovers_displayed_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When channels=None, auto-discovers displayed channels."""
    scope = _StubCaptureScope(
        trigger_polls_to_td=1,
        channels_displayed=[1, 3],
    )
    fn = _capture_tool(monkeypatch, scope)

    result = fn(sweep="SINGLE", timeout_s=5.0)

    assert set(result["channels"]) == {"CHAN1", "CHAN3"}
    assert "CHAN1" in result["waveform"]
    assert "CHAN3" in result["waveform"]


def test_scope_capture_rejects_negative_hysteresis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative hysteresis is rejected."""
    scope = _StubCaptureScope(channels_displayed=[1])
    fn = _capture_tool(monkeypatch, scope)
    with pytest.raises(ValueError, match="hysteresis_v"):
        fn(channels=["CHAN1"], hysteresis_v=-0.1)


def test_scope_capture_registered_on_server() -> None:
    """scope_capture is registered in the tool list."""
    srv = server_mod.build_server()
    names = {t.name for t in srv._tool_manager.list_tools()}
    assert "scope_capture" in names


# --- scope_compare --------------------------------------------------------


def _compare_tool(monkeypatch, scope=None):
    if scope is not None:
        monkeypatch.setattr(server_mod, "open_scope", lambda **kw: scope)
    srv = server_mod.build_server()
    return _registered_tool_callable(srv, "scope_compare")


def test_scope_compare_registered_on_server() -> None:
    """scope_compare is registered in the tool list."""
    srv = server_mod.build_server()
    names = {t.name for t in srv._tool_manager.list_tools()}
    assert "scope_compare" in names


def test_scope_compare_offline_with_hw_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline mode: pass hw_edges directly, no scope needed."""
    fn = _compare_tool(monkeypatch)

    ref_edges = [[1.0, "RISE"], [5.0, "FALL"], [9.0, "RISE"]]
    hw_edges = [[1.02, "RISE"], [5.01, "FALL"], [9.0, "RISE"]]

    result = fn(
        reference_edges=ref_edges,
        hw_edges=hw_edges,
        tolerance_us=0.05,
    )

    assert result["matched"] == 3
    assert result["missing"] == []
    assert result["added"] == []
    assert result["first_divergence_us"] is None
    assert "caveats" in result


def test_scope_compare_offline_with_reference_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline: reference as runs, hw as edges."""
    fn = _compare_tool(monkeypatch)

    # 3 runs: low(0-5), high(5-10), low(10-15) -> edges: RISE@5, FALL@10
    ref_runs = [[0.0, 0, 5.0], [5.0, 1, 5.0], [10.0, 0, 5.0]]
    hw_edges = [[5.01, "RISE"], [10.02, "FALL"]]

    result = fn(
        reference=ref_runs,
        hw_edges=hw_edges,
        tolerance_us=0.05,
    )

    assert result["matched"] == 2
    assert result["missing"] == []
    assert result["added"] == []


def test_scope_compare_offline_with_hw_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline: both reference and hw as runs."""
    fn = _compare_tool(monkeypatch)

    ref_runs = [[0.0, 0, 5.0], [5.0, 1, 5.0], [10.0, 0, 5.0]]
    hw_runs = [[0.0, 0, 5.02], [5.02, 1, 4.99], [10.01, 0, 4.99]]

    result = fn(
        reference=ref_runs,
        hw_runs=hw_runs,
        tolerance_us=0.05,
    )

    assert result["matched"] == 2
    assert result["missing"] == []
    assert result["added"] == []


def test_scope_compare_live_mode_with_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live mode: stub scope returns a canned waveform, compare against ref."""
    # Stub waveform: low,low,low,high,high,high,low,low at 1 us/pt
    # -> runs: [0,0,3], [3,1,3], [6,0,2] -> edges: RISE@3, FALL@6
    wf = Waveform(
        source="CHAN1",
        volts=[0, 0, 0, 3, 3, 3, 0, 0],
        t0_s=0.0,
        dt_s=1e-6,
    )
    scope = _StubWaveformScope(wf)
    fn = _compare_tool(monkeypatch, scope)

    # Reference: edges at exactly 3.0 and 6.0 us (perfect match)
    ref_edges = [[3.0, "RISE"], [6.0, "FALL"]]

    result = fn(
        reference_edges=ref_edges,
        source="CHAN1",
        threshold_v=1.5,
        hysteresis_v=0.1,
        tolerance_us=0.05,
    )

    assert result["matched"] == 2
    assert result["missing"] == []
    assert result["added"] == []
    assert result["first_divergence_us"] is None
    assert result["caveats"] == []


def test_scope_compare_live_mode_detects_shift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live mode: reference is shifted relative to HW capture."""
    wf = Waveform(
        source="CHAN1",
        volts=[0, 0, 0, 3, 3, 3, 0, 0],
        t0_s=0.0,
        dt_s=1e-6,
    )
    scope = _StubWaveformScope(wf)
    fn = _compare_tool(monkeypatch, scope)

    # Reference edges slightly off from actual (3.0, 6.0):
    ref_edges = [[3.02, "RISE"], [5.97, "FALL"]]

    result = fn(
        reference_edges=ref_edges,
        source="CHAN1",
        threshold_v=1.5,
        hysteresis_v=0.1,
        tolerance_us=0.05,
    )

    assert result["matched"] == 2
    # Verify delta signs.
    for s in result["shifted"]:
        if s["kind"] == "RISE":
            # hw=3.0, ref=3.02 -> delta = 3.0 - 3.02 = -0.02
            assert s["delta_us"] == pytest.approx(-0.02, abs=1e-6)


def test_scope_compare_no_reference_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Must provide at least one reference input."""
    fn = _compare_tool(monkeypatch)
    with pytest.raises(ValueError, match="reference"):
        fn(hw_edges=[[1.0, "RISE"]], tolerance_us=0.05)


def test_scope_compare_negative_tolerance_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative tolerance_us raises ValueError."""
    fn = _compare_tool(monkeypatch)
    with pytest.raises(ValueError, match="tolerance_us"):
        fn(reference_edges=[[1.0, "RISE"]], hw_edges=[[1.0, "RISE"]], tolerance_us=-0.01)
