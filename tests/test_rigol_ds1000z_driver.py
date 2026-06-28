"""Unit tests for the RIGOL DS1000Z driver — coordinate translation +
SCPI command sequencing against a fake transport.

The driver is exercised against a recording transport that captures
every SCPI command sent and replays canned responses. This is faster
and more deterministic than the in-process TCP server, and is sufficient
to cover the driver-level concerns: command shape, label sanitization,
cursor coordinate math, screenshot SCPI sequencing.
"""

from __future__ import annotations

from typing import Any

import pytest

from oscilloscope_mcp.helpers.measure import MeasureValidationError
from oscilloscope_mcp.helpers.trigger import TriggerValidationError
from oscilloscope_mcp.instruments import _load_profile
from oscilloscope_mcp.instruments._base import (
    CursorPair,
    ScreenshotPlan,
    TriggerSetup,
)
from oscilloscope_mcp.helpers.edges import edges_from_runs
from oscilloscope_mcp.helpers.quantize import quantize
from oscilloscope_mcp.helpers.rle import runs_from_levels
from oscilloscope_mcp.instruments.rigol_ds1000z import (
    RigolDs1000z,
    _normalize_source,
    _parse_preamble,
    _sanitize_ascii_label,
    _time_to_px,
    _volt_to_px,
)
from oscilloscope_mcp.transport.scpi_lan import ScpiError


# Canned responses so get_trigger() (also called at the end of
# set_trigger) parses a full edge-trigger state. The edge param queries
# use the long-form SCPI the engine builds from the profile subsystem.
_TRIG_EDGE_RESPONSES = {
    ":TRIG:MODE?": "EDGE",
    ":TRIG:STAT?": "STOP",
    ":TRIG:SWE?": "AUTO",
    ":TRIG:COUP?": "DC",
    ":TRIG:HOLD?": "8.000000e-09",
    ":TRIGger:EDGe:SOURce?": "CHAN1",
    ":TRIGger:EDGe:SLOPe?": "POS",
    ":TRIGger:EDGe:LEVel?": "1.500000e+00",
}


class _RecordingTransport:
    """In-memory SCPI transport that records writes and serves a query
    response table.
    """

    def __init__(self, response_map: dict[str, str] | None = None,
                 binary_response: bytes | None = None) -> None:
        self.writes: list[str] = []
        self.queries: list[str] = []
        self._responses = response_map or {}
        self._binary = binary_response or b"\x89PNG\r\n\x1a\n"

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)

    def write_many(self, cmds: list[str]) -> None:
        # Mirror the real transport's single-connection batch by recording
        # each command in order (no *OPC? — that's transport-internal).
        self.writes.extend(cmds)

    def query(self, cmd: str) -> str:
        self.queries.append(cmd)
        if cmd in self._responses:
            return self._responses[cmd]
        # Sensible defaults so tests not concerned with a particular
        # query don't have to enumerate them all.
        if cmd == "*IDN?":
            return "RIGOL TECHNOLOGIES,DS1104Z,SN,FW"
        if cmd == ":TIM:MAIN:SCAL?":
            return "1.000000e-06"  # 1 μs/div
        if cmd.endswith(":DISP?"):
            return "1"
        if cmd.endswith(":SCAL?"):
            return "1.000000e+00"  # 1 V/div
        if cmd.endswith(":OFFS?"):
            return "0.000000e+00"
        return ""

    def query_binary(self, cmd: str) -> bytes:
        self.queries.append(cmd)
        return self._binary


# Use the real merged profile so the driver exercises the actual trigger
# schema (15 types + ~95 params) rather than a hand-stubbed subset.
_PROFILE = _load_profile("rigol_ds1104z.yaml")


def _make_driver(**kw: Any) -> tuple[RigolDs1000z, _RecordingTransport]:
    t = _RecordingTransport(**kw)
    drv = RigolDs1000z(transport=t, profile=_PROFILE)
    drv.run_status_settle_s = 0  # no real-hardware settle in unit tests
    return drv, t


def test_idn_forwards_to_transport() -> None:
    drv, t = _make_driver()
    assert drv.idn() == "RIGOL TECHNOLOGIES,DS1104Z,SN,FW"
    assert "*IDN?" in t.queries


def test_query_raw_dispatches_by_question_mark() -> None:
    drv, t = _make_driver(response_map={":TRIG:STAT?": "STOP"})
    assert drv.query_raw(":TRIG:STAT?") == "STOP"
    assert drv.query_raw(":RUN") == ""
    assert ":RUN" in t.writes


def test_active_channel_count_counts_disp_on() -> None:
    drv, _ = _make_driver(response_map={
        ":CHAN1:DISP?": "1",
        ":CHAN2:DISP?": "0",
        ":CHAN3:DISP?": "1",
        ":CHAN4:DISP?": "1",
    })
    assert drv.active_channel_count() == 3


def test_screenshot_sets_labels_and_cursors_and_dumps_png() -> None:
    drv, t = _make_driver()
    plan = ScreenshotPlan(
        channel_labels={1: "DIR", 2: "STP"},
        cursor_pairs=[
            CursorPair(label="dir pulse", source_channel=1,
                       ax_t_s=-1e-6, bx_t_s=1e-6),
        ],
        image_format="PNG",
    )
    result = drv.screenshot(plan)

    # Labels were sent first.
    assert ':CHAN1:LAB "DIR"' in t.writes
    assert ':CHAN2:LAB "STP"' in t.writes
    assert ":DISP:LABS ON" in t.writes
    # Cursor SCPI was emitted in MANUAL mode against CH1.
    assert ":CURS:MODE MAN" in t.writes
    assert ":CURS:MAN:SOUR CHAN1" in t.writes
    # AX corresponds to t = -1 μs at 1 μs/div → screen center - 50 px = 250.
    assert ":CURS:MAN:AX 250" in t.writes
    assert ":CURS:MAN:BX 350" in t.writes
    # PNG SCPI was issued.
    assert any(cmd.startswith(":DISP:DATA? ON,0,PNG") for cmd in t.queries)
    # Result echoes labels + cursors.
    assert result.channel_labels_applied == {1: "DIR", 2: "STP"}
    assert len(result.cursors_set) == 1
    assert result.image_format == "PNG"


def test_screenshot_rejects_unsupported_format() -> None:
    drv, _ = _make_driver()
    with pytest.raises(ValueError, match="unsupported screenshot format"):
        drv.screenshot(ScreenshotPlan(image_format="JPEG"))


def test_screenshot_sanitizes_non_ascii_and_truncates_label() -> None:
    drv, t = _make_driver()
    drv.screenshot(ScreenshotPlan(channel_labels={1: "日本語LABEL_HUGE"}))
    sent = next(cmd for cmd in t.writes if cmd.startswith(":CHAN1:LAB"))
    # ASCII chars from the input, truncated to 10.
    assert sent == ':CHAN1:LAB "LABEL_HUGE"'


@pytest.mark.parametrize("t_s,t_per_div,expected_px", [
    (0.0, 1e-6, 300),     # center
    (1e-6, 1e-6, 350),    # +1 div right
    (-2e-6, 1e-6, 200),   # -2 div left
])
def test_time_to_px(t_s: float, t_per_div: float, expected_px: int) -> None:
    assert _time_to_px(t_s, t_per_div) == expected_px


@pytest.mark.parametrize("v,v_per_div,offset_v,expected_px", [
    (0.0, 1.0, 0.0, 200),     # center
    (1.0, 1.0, 0.0, 150),     # +1 div above center (pixels decrease upward)
    (-2.0, 1.0, 0.0, 300),    # -2 div below center
])
def test_volt_to_px(v: float, v_per_div: float, offset_v: float, expected_px: int) -> None:
    assert _volt_to_px(v, v_per_div, offset_v) == expected_px


def test_time_to_px_rejects_non_positive_timebase() -> None:
    with pytest.raises(ValueError):
        _time_to_px(0.0, 0.0)


def test_sanitize_ascii_label_strips_quote() -> None:
    assert _sanitize_ascii_label('hello"world') == "helloworld"


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


def test_get_trigger_reads_global_and_mode_params() -> None:
    drv, _ = _make_driver(response_map=_TRIG_EDGE_RESPONSES)
    state = drv.get_trigger()
    assert state.mode == "EDGE"
    assert state.status == "STOP"
    assert state.sweep == "AUTO"
    assert state.coupling == "DC"
    assert state.holdoff_s == pytest.approx(8e-9)
    # Mode-specific params read generically from the profile schema.
    assert state.params["source"] == "CHAN1"
    assert state.params["slope"] == "POS"
    assert state.params["level_v"] == pytest.approx(1.5)


def test_get_trigger_reads_correct_subsystem_for_non_edge() -> None:
    resp = {
        ":TRIG:MODE?": "NEDG",
        ":TRIGger:NEDGe:SOURce?": "CHAN1",
        ":TRIGger:NEDGe:SLOPe?": "NEG",
        ":TRIGger:NEDGe:EDGE?": "2",
        ":TRIGger:NEDGe:IDLE?": "9.000000e-07",
        ":TRIGger:NEDGe:LEVel?": "0.000000e+00",
    }
    drv, t = _make_driver(response_map=resp)
    state = drv.get_trigger()
    assert state.mode == "NEDG"                 # short form normalized
    assert state.params["nth_edge"] == 2        # parsed as int
    assert state.params["slope"] == "NEG"
    assert state.params["idle_s"] == pytest.approx(9e-7)
    # It must query the NEDG subsystem, not EDGE.
    assert ":TRIGger:NEDGe:EDGE?" in t.queries
    assert ":TRIGger:EDGe:SOURce?" not in t.queries


def test_set_trigger_edge_full_sequence() -> None:
    drv, t = _make_driver(response_map=_TRIG_EDGE_RESPONSES)
    drv.set_trigger(TriggerSetup(
        mode="EDGE", sweep="single", coupling="dc", holdoff_s=1e-7,
        params={"source": "1", "slope": "rising", "level_v": 1.5},
    ))
    # Mode/global are set before the param writes that depend on the mode.
    assert t.writes.index(":TRIG:MODE EDGE") < t.writes.index(":TRIGger:EDGe:SOURce CHAN1")
    assert ":TRIG:SWE SINGLE" in t.writes
    assert ":TRIG:COUP DC" in t.writes
    assert ":TRIG:HOLD 1e-07" in t.writes
    assert ":TRIGger:EDGe:SOURce CHAN1" in t.writes    # "1" → CHAN1
    assert ":TRIGger:EDGe:SLOPe POSitive" in t.writes  # "rising" alias
    assert ":TRIGger:EDGe:LEVel 1.5" in t.writes


def test_set_trigger_nth_edge_params() -> None:
    """The headline case: CH1 falling edge, 2nd — all via typed params."""
    drv, t = _make_driver(response_map={":TRIG:MODE?": "NEDG"})
    drv.set_trigger(TriggerSetup(
        mode="NEDG",
        params={"source": "CHAN1", "slope": "NEG", "nth_edge": 2, "idle_s": 2e-3},
    ))
    assert ":TRIG:MODE NEDG" in t.writes
    assert ":TRIGger:NEDGe:SOURce CHAN1" in t.writes
    assert ":TRIGger:NEDGe:SLOPe NEGative" in t.writes
    assert ":TRIGger:NEDGe:EDGE 2" in t.writes
    assert ":TRIGger:NEDGe:IDLE 0.002" in t.writes


def test_set_trigger_mode_only() -> None:
    resp = dict(_TRIG_EDGE_RESPONSES, **{":TRIG:MODE?": "PULS"})
    drv, t = _make_driver(response_map=resp)
    state = drv.set_trigger(TriggerSetup(mode="PULSe"))
    assert t.writes == [":TRIG:MODE PULSe"]
    assert state.mode == "PULSe"


def test_set_trigger_rejects_unknown_mode_before_any_write() -> None:
    drv, t = _make_driver(response_map=_TRIG_EDGE_RESPONSES)
    with pytest.raises(TriggerValidationError, match="not supported"):
        drv.set_trigger(TriggerSetup(mode="BOGUS"))
    assert t.writes == []  # validation happens before any SCPI is sent


def test_set_trigger_rejects_unknown_param_before_any_write() -> None:
    drv, t = _make_driver(response_map=_TRIG_EDGE_RESPONSES)
    with pytest.raises(TriggerValidationError, match="not valid for trigger mode"):
        drv.set_trigger(TriggerSetup(mode="EDGE", params={"nth_edge": 2}))
    assert t.writes == []  # bad param for EDGE → nothing sent


def test_set_trigger_validates_params_against_target_mode() -> None:
    # nth_edge is valid for NEDG; out-of-range value is rejected pre-write.
    drv, t = _make_driver(response_map={":TRIG:MODE?": "NEDG"})
    with pytest.raises(TriggerValidationError, match="above maximum"):
        drv.set_trigger(TriggerSetup(mode="NEDG", params={"nth_edge": 999999}))
    assert t.writes == []


def test_set_trigger_invalid_sweep_rejected() -> None:
    drv, _ = _make_driver(response_map=_TRIG_EDGE_RESPONSES)
    with pytest.raises(TriggerValidationError):
        drv.set_trigger(TriggerSetup(sweep="CONTINUOUS"))


@pytest.mark.parametrize("action,scpi", [
    ("RUN", ":RUN"), ("STOP", ":STOP"), ("SINGLE", ":SINGle"), ("FORCE", ":TFORce"),
])
def test_run_control_writes_root_command(action: str, scpi: str) -> None:
    drv, t = _make_driver()
    assert drv.run_control(action) == action
    assert scpi in t.writes


def test_run_control_rejects_unknown_action() -> None:
    drv, t = _make_driver()
    with pytest.raises(TriggerValidationError, match="valid:"):
        drv.run_control("PAUSE")
    assert t.writes == []


# ---------------------------------------------------------------------------
# Automatic measurements (:MEAS:ITEM?)
# ---------------------------------------------------------------------------


def test_measure_emits_correct_scpi_per_item() -> None:
    resp = {
        ":MEAS:ITEM? FREQuency,CHANnel1": "1.000000e+03",
        ":MEAS:ITEM? VPP,CHANnel1": "3.300000e+00",
    }
    drv, t = _make_driver(response_map=resp)
    out = drv.measure(["FREQUENCY", "VPP"], source="CHAN1")
    # One :MEAS:ITEM? per item, using the profile's SCPI keyword + CHANnel<n>.
    assert ":MEAS:ITEM? FREQuency,CHANnel1" in t.queries
    assert ":MEAS:ITEM? VPP,CHANnel1" in t.queries
    assert out["FREQUENCY"] == pytest.approx(1000.0)
    assert out["VPP"] == pytest.approx(3.3)


def test_measure_normalizes_bare_channel_source() -> None:
    resp = {":MEAS:ITEM? VMAX,CHANnel2": "5.000000e+00"}
    drv, t = _make_driver(response_map=resp)
    out = drv.measure(["VMAX"], source="2")  # bare number → CHANnel2
    assert ":MEAS:ITEM? VMAX,CHANnel2" in t.queries
    assert out["VMAX"] == pytest.approx(5.0)


def test_measure_sentinel_parses_to_none() -> None:
    resp = {":MEAS:ITEM? FREQuency,CHANnel1": "9.900000e+37"}
    drv, _ = _make_driver(response_map=resp)
    out = drv.measure(["FREQUENCY"], source="CHAN1")
    assert out["FREQUENCY"] is None  # un-measurable sentinel → None


def test_measure_dual_source_item_sends_both_sources() -> None:
    resp = {":MEAS:ITEM? RDELay,CHANnel1,CHANnel2": "2.500000e-08"}
    drv, t = _make_driver(response_map=resp)
    out = drv.measure(["RDELAY"], source="CHAN1", source2="CHAN2")
    assert ":MEAS:ITEM? RDELay,CHANnel1,CHANnel2" in t.queries
    assert out["RDELAY"] == pytest.approx(2.5e-8)


def test_measure_dual_source_without_source2_rejected_before_any_query() -> None:
    drv, t = _make_driver()
    with pytest.raises(MeasureValidationError, match="requires a second source"):
        drv.measure(["RDELAY"], source="CHAN1")
    # Validation happens before any SCPI query.
    assert not any(q.startswith(":MEAS:ITEM?") for q in t.queries)


def test_measure_rejects_unknown_item_before_any_query() -> None:
    drv, t = _make_driver()
    with pytest.raises(MeasureValidationError, match="not supported"):
        drv.measure(["NOPE"], source="CHAN1")
    assert not any(q.startswith(":MEAS:ITEM?") for q in t.queries)


def test_measure_rejects_out_of_range_source_before_any_query() -> None:
    drv, t = _make_driver()
    with pytest.raises(MeasureValidationError, match="out of range"):
        drv.measure(["VPP"], source="9")
    assert not any(q.startswith(":MEAS:ITEM?") for q in t.queries)


def test_measure_scpi_error_on_one_item_yields_none_not_failure() -> None:
    class _FlakyTransport(_RecordingTransport):
        def query(self, cmd: str) -> str:
            if cmd == ":MEAS:ITEM? VPP,CHANnel1":
                from oscilloscope_mcp.transport.scpi_lan import ScpiError
                raise ScpiError("boom")
            return super().query(cmd)

    t = _FlakyTransport(response_map={":MEAS:ITEM? FREQuency,CHANnel1": "1.0e3"})
    drv = RigolDs1000z(transport=t, profile=_PROFILE)
    out = drv.measure(["FREQUENCY", "VPP"], source="CHAN1")
    assert out["FREQUENCY"] == pytest.approx(1000.0)
    assert out["VPP"] is None  # transport error on one item → null, no raise


# ---------------------------------------------------------------------------
# Measurement statistics (:MEAS:STAT)
# ---------------------------------------------------------------------------


def test_measure_statistics_enables_display_and_queries_all_stat_types() -> None:
    resp = {
        ":MEAS:STAT:ITEM? CURRent,FREQuency,CHANnel1": "1.000000e+03",
        ":MEAS:STAT:ITEM? MAXimum,FREQuency,CHANnel1": "1.001000e+03",
        ":MEAS:STAT:ITEM? MINimum,FREQuency,CHANnel1": "9.990000e+02",
        ":MEAS:STAT:ITEM? AVERages,FREQuency,CHANnel1": "1.000100e+03",
        ":MEAS:STAT:ITEM? DEViation,FREQuency,CHANnel1": "5.000000e-01",
        ":MEAS:STAT:ITEM? COUNt,FREQuency,CHANnel1": "42",
    }
    drv, t = _make_driver(response_map=resp)
    results = drv.measure_statistics(["FREQUENCY"], source="CHAN1")

    # Must enable statistics display first.
    assert ":MEAS:STAT:DISP ON" in t.writes

    # Should have queried all six stat types.
    assert ":MEAS:STAT:ITEM? CURRent,FREQuency,CHANnel1" in t.queries
    assert ":MEAS:STAT:ITEM? MAXimum,FREQuency,CHANnel1" in t.queries
    assert ":MEAS:STAT:ITEM? MINimum,FREQuency,CHANnel1" in t.queries
    assert ":MEAS:STAT:ITEM? AVERages,FREQuency,CHANnel1" in t.queries
    assert ":MEAS:STAT:ITEM? DEViation,FREQuency,CHANnel1" in t.queries
    assert ":MEAS:STAT:ITEM? COUNt,FREQuency,CHANnel1" in t.queries

    assert len(results) == 1
    r = results[0]
    assert r.item == "FREQUENCY"
    assert r.source == "CHANnel1"
    assert r.current == pytest.approx(1000.0)
    assert r.maximum == pytest.approx(1001.0)
    assert r.minimum == pytest.approx(999.0)
    assert r.average == pytest.approx(1000.1)
    assert r.deviation == pytest.approx(0.5)
    assert r.count == 42


def test_measure_statistics_sentinel_parses_to_none() -> None:
    resp = {
        ":MEAS:STAT:ITEM? CURRent,FREQuency,CHANnel1": "9.900000e+37",
        ":MEAS:STAT:ITEM? MAXimum,FREQuency,CHANnel1": "9.900000e+37",
        ":MEAS:STAT:ITEM? MINimum,FREQuency,CHANnel1": "9.900000e+37",
        ":MEAS:STAT:ITEM? AVERages,FREQuency,CHANnel1": "9.900000e+37",
        ":MEAS:STAT:ITEM? DEViation,FREQuency,CHANnel1": "9.900000e+37",
        ":MEAS:STAT:ITEM? COUNt,FREQuency,CHANnel1": "9.900000e+37",
    }
    drv, _ = _make_driver(response_map=resp)
    results = drv.measure_statistics(["FREQUENCY"], source="CHAN1")
    r = results[0]
    assert r.current is None
    assert r.maximum is None
    assert r.minimum is None
    assert r.average is None
    assert r.deviation is None
    assert r.count is None


def test_measure_statistics_count_parsed_as_int() -> None:
    resp = {
        ":MEAS:STAT:ITEM? COUNt,VPP,CHANnel1": "100",
    }
    drv, _ = _make_driver(response_map=resp)
    results = drv.measure_statistics(["VPP"], source="CHAN1", stat_types=["COUNt"])
    assert results[0].count == 100
    assert isinstance(results[0].count, int)


def test_measure_statistics_subset_of_stat_types() -> None:
    resp = {
        ":MEAS:STAT:ITEM? CURRent,VPP,CHANnel1": "3.300000e+00",
        ":MEAS:STAT:ITEM? MAXimum,VPP,CHANnel1": "3.500000e+00",
    }
    drv, t = _make_driver(response_map=resp)
    results = drv.measure_statistics(
        ["VPP"], source="CHAN1", stat_types=["CURRent", "MAXimum"]
    )
    # Should only query the requested stat types.
    stat_queries = [q for q in t.queries if q.startswith(":MEAS:STAT:ITEM?")]
    assert len(stat_queries) == 2
    assert results[0].current == pytest.approx(3.3)
    assert results[0].maximum == pytest.approx(3.5)
    assert results[0].minimum is None  # not requested
    assert results[0].average is None


def test_measure_statistics_rejects_unknown_item() -> None:
    drv, t = _make_driver()
    with pytest.raises(MeasureValidationError, match="not supported"):
        drv.measure_statistics(["BOGUS"], source="CHAN1")
    # Validation before any SCPI.
    assert ":MEAS:STAT:DISP ON" not in t.writes


def test_measure_statistics_rejects_out_of_range_source() -> None:
    drv, t = _make_driver()
    with pytest.raises(MeasureValidationError, match="out of range"):
        drv.measure_statistics(["VPP"], source="9")
    assert ":MEAS:STAT:DISP ON" not in t.writes


# ---------------------------------------------------------------------------
# Waveform capture
# ---------------------------------------------------------------------------


# A canonical DS1000Z NORMal-mode preamble: BYTE format, 8 points, 1 µs/pt,
# trigger (xreference) at sample 0, 1 V per ADC count, yorigin/yref = 0.
#   format,type,points,count,xinc,xorigin,xref,yinc,yorigin,yref
_WAV_PREAMBLE = "0,0,8,1,1.000000e-06,0.000000e+00,0,1.000000e+00,0,0"

# Eight BYTE samples forming a low-high-low pattern: 0,0,0,3,3,3,0,0 volts
# (yincrement = 1 V/count, no offset → raw count == volts).
_WAV_BYTES = bytes([0, 0, 0, 3, 3, 3, 0, 0])


def test_parse_preamble_typed_fields() -> None:
    pre = _parse_preamble(_WAV_PREAMBLE)
    assert pre["points"] == 8
    assert pre["xincrement"] == pytest.approx(1e-6)
    assert pre["yincrement"] == pytest.approx(1.0)
    assert pre["xreference"] == 0


def test_parse_preamble_rejects_short_response() -> None:
    with pytest.raises(ScpiError, match="PREamble"):
        _parse_preamble("0,0,8")


@pytest.mark.parametrize("src,expected", [
    ("1", "CHAN1"), ("ch2", "CHAN2"), ("CHAN3", "CHAN3"), ("channel4", "CHAN4"),
])
def test_normalize_source(src: str, expected: str) -> None:
    assert _normalize_source(src, 4) == expected


def test_normalize_source_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of range"):
        _normalize_source("CHAN5", 4)


def test_normalize_source_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="invalid"):
        _normalize_source("FOO", 4)


def test_read_waveform_sets_mode_and_scales_volts() -> None:
    drv, t = _make_driver(
        response_map={":WAV:PREamble?": _WAV_PREAMBLE},
        binary_response=_WAV_BYTES,
    )
    wf = drv.read_waveform("CHAN1")

    # Configured source + read mode/format before reading the data.
    assert ":WAV:SOURce CHAN1" in t.writes
    assert ":WAV:MODE NORMal" in t.writes
    assert ":WAV:FORMat BYTE" in t.writes
    assert ":WAV:DATA?" in t.queries

    assert wf.source == "CHAN1"
    assert wf.n_samples == 8
    # raw - yorigin(0) - yref(0)) * yinc(1) → volts == raw count.
    assert wf.volts == pytest.approx([0, 0, 0, 3, 3, 3, 0, 0])
    assert wf.dt_s == pytest.approx(1e-6)
    # time[0] = (0 - xref(0)) * xinc + xorigin(0) = 0.
    assert wf.t0_s == pytest.approx(0.0)


def test_read_waveform_applies_yorigin_yref_offset() -> None:
    # yincrement 0.04 V/count, yorigin 125, yreference 0 → a mid-scale
    # count of 125 maps to 0 V, 255 maps to +5.2 V.
    pre = "0,0,3,1,1.000000e-06,0.000000e+00,0,4.000000e-02,125,0"
    drv, _ = _make_driver(
        response_map={":WAV:PREamble?": pre},
        binary_response=bytes([125, 130, 120]),
    )
    wf = drv.read_waveform("CHAN2")
    assert wf.volts == pytest.approx([0.0, 0.2, -0.2])


# ---------------------------------------------------------------------------
# Acquisition mode (acquire type / averages / memory depth)
# ---------------------------------------------------------------------------


_ACQ_RESPONSES = {
    ":ACQ:TYPE?": "NORM",
    ":ACQ:AVER?": "2",
    ":ACQ:MDEP?": "AUTO",
    ":ACQ:SRAT?": "1.000000e+09",
    ":CHAN1:DISP?": "1",
    ":CHAN2:DISP?": "0",
    ":CHAN3:DISP?": "0",
    ":CHAN4:DISP?": "0",
}


def test_get_acquire_reads_all_fields() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    state = drv.get_acquire()
    assert state.type == "NORM"
    assert state.averages == 2
    assert state.memory_depth == "AUTO"
    assert state.sample_rate == pytest.approx(1e9)
    assert ":ACQ:TYPE?" in t.queries
    assert ":ACQ:AVER?" in t.queries
    assert ":ACQ:MDEP?" in t.queries
    assert ":ACQ:SRAT?" in t.queries


def test_get_acquire_numeric_memory_depth() -> None:
    resp = dict(_ACQ_RESPONSES, **{":ACQ:MDEP?": "12000000"})
    drv, _ = _make_driver(response_map=resp)
    state = drv.get_acquire()
    assert state.memory_depth == 12000000


def test_set_acquire_type_emits_correct_scpi() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    from oscilloscope_mcp.instruments._base import AcquireSetup
    drv.set_acquire(AcquireSetup(type="AVERages"))
    assert ":ACQ:TYPE AVERages" in t.writes


def test_set_acquire_averages_emits_correct_scpi() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    from oscilloscope_mcp.instruments._base import AcquireSetup
    drv.set_acquire(AcquireSetup(averages=64))
    assert ":ACQ:AVER 64" in t.writes


def test_set_acquire_memory_depth_auto_emits_correct_scpi() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    from oscilloscope_mcp.instruments._base import AcquireSetup
    drv.set_acquire(AcquireSetup(memory_depth="AUTO"))
    assert ":ACQ:MDEP AUTO" in t.writes


def test_set_acquire_memory_depth_numeric_emits_correct_scpi() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    from oscilloscope_mcp.instruments._base import AcquireSetup
    drv.set_acquire(AcquireSetup(memory_depth=24000000))
    assert ":ACQ:MDEP 24000000" in t.writes


def test_set_acquire_full_burst() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    from oscilloscope_mcp.instruments._base import AcquireSetup
    drv.set_acquire(AcquireSetup(type="PEAK", averages=8, memory_depth=12000))
    assert ":ACQ:TYPE PEAK" in t.writes
    assert ":ACQ:AVER 8" in t.writes
    assert ":ACQ:MDEP 12000" in t.writes
    # Type must come before averages in the burst.
    assert t.writes.index(":ACQ:TYPE PEAK") < t.writes.index(":ACQ:AVER 8")


def test_set_acquire_rejects_invalid_type_before_write() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    from oscilloscope_mcp.instruments._base import AcquireSetup
    from oscilloscope_mcp.helpers.acquisition import AcquisitionValidationError
    with pytest.raises(AcquisitionValidationError, match="invalid"):
        drv.set_acquire(AcquireSetup(type="BOGUS"))
    assert t.writes == []


def test_set_acquire_rejects_invalid_averages_before_write() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    from oscilloscope_mcp.instruments._base import AcquireSetup
    from oscilloscope_mcp.helpers.acquisition import AcquisitionValidationError
    with pytest.raises(AcquisitionValidationError, match="invalid"):
        drv.set_acquire(AcquireSetup(averages=3))
    assert t.writes == []


def test_set_acquire_rejects_invalid_memory_depth_before_write() -> None:
    drv, t = _make_driver(response_map=_ACQ_RESPONSES)
    from oscilloscope_mcp.instruments._base import AcquireSetup
    from oscilloscope_mcp.helpers.acquisition import AcquisitionValidationError
    with pytest.raises(AcquisitionValidationError, match="invalid"):
        drv.set_acquire(AcquireSetup(memory_depth=99999))
    assert t.writes == []


def test_read_waveform_pipeline_produces_runs_and_edges() -> None:
    """End-to-end driver → helpers: the canned waveform yields one clean
    high pulse → one RISE then one FALL edge."""
    drv, _ = _make_driver(
        response_map={":WAV:PREamble?": _WAV_PREAMBLE},
        binary_response=_WAV_BYTES,
    )
    wf = drv.read_waveform("CHAN1")

    levels = quantize(wf.volts, threshold_v=1.5, hysteresis_v=0.1)
    runs = runs_from_levels(levels, t0_us=wf.t0_s * 1e6, dt_us=wf.dt_s * 1e6)
    edges = edges_from_runs(runs, wf.source)

    # 0,0,0 (low) | 3,3,3 (high) | 0,0 (low) → 3 runs at 1 µs/sample.
    assert [r.level for r in runs] == [0, 1, 0]
    assert runs[1].t_us == pytest.approx(3.0)   # rising at sample 3 = 3 µs
    assert runs[1].dur_us == pytest.approx(3.0)
    assert [(e.t_us, e.kind) for e in edges] == [(3.0, "RISE"), (6.0, "FALL")]


# ---------------------------------------------------------------------------
# RAW waveform capture (full memory depth)
# ---------------------------------------------------------------------------


class _ChunkedBinaryTransport(_RecordingTransport):
    """Transport that returns sequential binary chunks for :WAV:DATA? calls.

    Each call to query_binary pops the next chunk from a FIFO, allowing
    tests to simulate multi-chunk RAW reads.
    """

    def __init__(
        self,
        response_map: dict[str, str] | None = None,
        binary_chunks: list[bytes] | None = None,
    ) -> None:
        super().__init__(response_map=response_map)
        self._binary_chunks = list(binary_chunks or [])
        self._binary_index = 0

    def query_binary(self, cmd: str) -> bytes:
        self.queries.append(cmd)
        if self._binary_index < len(self._binary_chunks):
            chunk = self._binary_chunks[self._binary_index]
            self._binary_index += 1
            return chunk
        return b""


def _make_chunked_driver(
    response_map: dict[str, str] | None = None,
    binary_chunks: list[bytes] | None = None,
) -> tuple[RigolDs1000z, _ChunkedBinaryTransport]:
    t = _ChunkedBinaryTransport(
        response_map=response_map, binary_chunks=binary_chunks
    )
    drv = RigolDs1000z(transport=t, profile=_PROFILE)
    drv.run_status_settle_s = 0
    return drv, t


# RAW preamble: 500000 points, 1 ns/pt (1 GSa/s), yinc=0.04, yorigin=125, yref=0.
_RAW_PREAMBLE = "0,2,500000,1,1.000000e-09,0.000000e+00,0,4.000000e-02,125,0"


def test_read_waveform_raw_chunked_reads() -> None:
    """RAW mode with 500000 points requires 2 chunks (250000 each).
    Verify correct chunk windowing and voltage scaling."""
    # Create two 250000-byte chunks (all at ADC count 125 → 0 V).
    chunk1 = bytes([125]) * 250000
    chunk2 = bytes([225]) * 250000  # (225 - 125 - 0) * 0.04 = 4.0 V

    drv, t = _make_chunked_driver(
        response_map={
            ":TRIG:STAT?": "STOP",
            ":WAV:PREamble?": _RAW_PREAMBLE,
        },
        binary_chunks=[chunk1, chunk2],
    )
    wf = drv.read_waveform("CHAN1", mode="RAW")

    # Verify setup commands.
    assert ":WAV:SOURce CHAN1" in t.writes
    assert ":WAV:MODE RAW" in t.writes
    assert ":WAV:FORMat BYTE" in t.writes

    # Verify chunked read: two :WAV:DATA? calls.
    data_queries = [q for q in t.queries if q == ":WAV:DATA?"]
    assert len(data_queries) == 2

    # Verify START/STOP windowing commands.
    assert ":WAV:STARt 1" in t.writes
    assert ":WAV:STOP 250000" in t.writes
    assert ":WAV:STARt 250001" in t.writes
    assert ":WAV:STOP 500000" in t.writes

    # Verify total samples.
    assert wf.n_samples == 500000
    assert wf.source == "CHAN1"

    # First 250000 samples: (125 - 125 - 0) * 0.04 = 0.0 V
    assert wf.volts[0] == pytest.approx(0.0)
    assert wf.volts[249999] == pytest.approx(0.0)

    # Last 250000 samples: (225 - 125 - 0) * 0.04 = 4.0 V
    assert wf.volts[250000] == pytest.approx(4.0)
    assert wf.volts[499999] == pytest.approx(4.0)

    # Timing: dt = 1 ns, t0 = 0.
    assert wf.dt_s == pytest.approx(1e-9)
    assert wf.t0_s == pytest.approx(0.0)


def test_read_waveform_raw_single_chunk() -> None:
    """RAW mode with points <= 250000 requires only one chunk."""
    # 100000 points, all at count 150 → (150 - 125 - 0) * 0.04 = 1.0 V
    preamble = "0,2,100000,1,1.000000e-09,0.000000e+00,0,4.000000e-02,125,0"
    chunk = bytes([150]) * 100000

    drv, t = _make_chunked_driver(
        response_map={
            ":TRIG:STAT?": "STOP",
            ":WAV:PREamble?": preamble,
        },
        binary_chunks=[chunk],
    )
    wf = drv.read_waveform("CHAN1", mode="RAW")

    # Only one chunk.
    data_queries = [q for q in t.queries if q == ":WAV:DATA?"]
    assert len(data_queries) == 1
    assert ":WAV:STARt 1" in t.writes
    assert ":WAV:STOP 100000" in t.writes

    assert wf.n_samples == 100000
    assert wf.volts[0] == pytest.approx(1.0)


def test_read_waveform_raw_rejects_running_scope() -> None:
    """RAW mode raises RuntimeError if the scope is not stopped."""
    drv, t = _make_chunked_driver(
        response_map={":TRIG:STAT?": "WAIT"},
    )
    with pytest.raises(RuntimeError, match="scope must be stopped"):
        drv.read_waveform("CHAN1", mode="RAW")
    # No WAV commands should have been sent.
    assert ":WAV:MODE RAW" not in t.writes


def test_read_waveform_raw_accepts_td_status() -> None:
    """RAW mode works when trigger status is TD (triggered, scope stopped)."""
    preamble = "0,2,8,1,1.000000e-06,0.000000e+00,0,1.000000e+00,0,0"
    chunk = bytes([0, 0, 0, 3, 3, 3, 0, 0])

    drv, t = _make_chunked_driver(
        response_map={
            ":TRIG:STAT?": "TD",
            ":WAV:PREamble?": preamble,
        },
        binary_chunks=[chunk],
    )
    wf = drv.read_waveform("CHAN1", mode="RAW")
    assert wf.n_samples == 8
    assert wf.volts == pytest.approx([0, 0, 0, 3, 3, 3, 0, 0])


def test_read_waveform_mode_invalid_raises_valueerror() -> None:
    """An invalid mode string raises ValueError."""
    drv, _ = _make_driver(
        response_map={":WAV:PREamble?": _WAV_PREAMBLE},
        binary_response=_WAV_BYTES,
    )
    with pytest.raises(ValueError, match="unsupported waveform mode"):
        drv.read_waveform("CHAN1", mode="BOGUS")


def test_read_waveform_normal_mode_explicit() -> None:
    """Explicitly passing mode='NORMal' works the same as the default."""
    drv, t = _make_driver(
        response_map={":WAV:PREamble?": _WAV_PREAMBLE},
        binary_response=_WAV_BYTES,
    )
    wf = drv.read_waveform("CHAN1", mode="NORMal")
    assert wf.n_samples == 8
    assert ":WAV:MODE NORMal" in t.writes
