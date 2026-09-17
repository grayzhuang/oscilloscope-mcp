"""Unit tests for the ZLG ZDS1000 driver — no hardware (fake transport).

Mirrors ``test_rigol_ds1000z_driver.py``: a recording fake transport
asserts the exact ZDS SCPI spellings, and validation failures must
leave ``writes`` empty (rejected before any SCPI is sent). The WFM
parser is exercised against synthetic byte streams built with the same
structs the driver uses.
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers.acquisition import AcquisitionValidationError
from oscilloscope_mcp.helpers.measure import MeasureValidationError
from oscilloscope_mcp.helpers.trigger import TriggerValidationError
from oscilloscope_mcp.instruments import _load_profile
from oscilloscope_mcp.instruments._base import (
    AcquireSetup,
    ChannelSetup,
    CursorPair,
    ScreenshotPlan,
    TimebaseSetup,
    TriggerSetup,
)
from oscilloscope_mcp.instruments.zlg_zds1000 import (
    ZlgZds1000,
    _WFM_HEAD,
    _WFM_ITEM,
    _parse_wfm,
)
from oscilloscope_mcp.transport.scpi_lan import ScpiEmptyBlockError, ScpiError


class _RecordingTransport:
    """Duck-typed stand-in for ScpiLan that records every write/query."""

    def __init__(self, response_map: dict[str, str] | None = None,
                 binary_response: bytes | None = None,
                 lp_binary_response: bytes | None = None) -> None:
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.timeout_s = 3.0
        self._responses = response_map or {}
        self._binary = binary_response or b"BM\x36\x00\x00\x00" + b"\x00" * 64
        self._lp_binary = lp_binary_response or b""

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)

    def write_many(self, cmds: list[str]) -> None:
        self.writes.extend(cmds)

    def query(self, cmd: str) -> str:
        self.queries.append(cmd)
        if cmd in self._responses:
            return self._responses[cmd]
        if cmd == "*IDN?":
            return "ZHIYUANELECT,ZDS1104,SN0001,V1.0,1.0.0"
        if cmd == ":GLOBal:RUN:STATe?":
            return "Stop"
        if cmd == ":TRIGger:MODE?":
            return "EDGE"
        if cmd == ":SYSTem:VERSion?":
            return "1999.0"
        if cmd == ":TIMebase:MODE?":
            return "MAIN"
        if cmd.endswith(":DISPlay?"):
            return "1"
        if cmd.endswith(":SCALe?"):
            return "1.000000E-3"
        if cmd.endswith((":OFFSet?", ":OFFS?")):
            return "0.000000E+0"
        if cmd.endswith(":PROBe?"):
            return "10"
        if cmd.endswith(":COUPling?"):
            return "DC"
        if cmd.endswith(":BWLimit?"):
            return "OFF"
        if cmd.endswith(":INVert?"):
            return "0"
        if cmd.endswith(":UNITs?"):
            return "VOLTage"
        if cmd.endswith(":TYPE?"):
            return "NORMal"
        if cmd.endswith(":AVERages?"):
            return "64"
        if cmd.endswith(":MDEPth?"):
            return "1400"
        if cmd.endswith(":SRATe?"):
            return "1.000000E+9"
        if cmd.endswith(":SWEep?"):
            return "AUTO"
        if cmd.endswith(":HOLDoff?"):
            return "0"
        if cmd.endswith(":SOURce?"):
            return "CH1"
        if cmd.endswith(":SLOPe?"):
            return "POSitive"
        if cmd.endswith(":LEVel?"):
            return "0.000000E+0"
        return ""

    def query_binary(self, cmd: str, recv_max: int = 4 * 1024 * 1024) -> bytes:
        self.queries.append(cmd)
        return self._binary

    def query_length_prefixed(self, cmd: str,
                              recv_max: int = 64 * 1024 * 1024) -> bytes:
        self.queries.append(cmd)
        return self._lp_binary


_PROFILE = _load_profile("zlg_zds1104.yaml")


def _make_driver(**kw) -> tuple[ZlgZds1000, _RecordingTransport]:
    t = _RecordingTransport(**kw)
    drv = ZlgZds1000(transport=t, profile=_PROFILE)
    drv.run_status_settle_s = 0
    return drv, t


def _wfm_payload(channel: int = 1, samples: bytes = b"\x80\x96\xa0",
                 sample_rate: float = 1.0e9, vert_div: float = 1.0e-3,
                 n_items: int = 1) -> bytes:
    """Build a synthetic WFM stream: Head + Item[n] + Data."""
    data_off = _WFM_HEAD.size + n_items * _WFM_ITEM.size
    head = _WFM_HEAD.pack(
        b"WFM\x00", b"ZDS1104", b"V1.0", b"V1.00", 0,
        1.0e-3, 0.0, 0.0, 0, 0, sample_rate, 0, 0,
        b"EDGE", n_items, *([0] * 17),
    )
    item = _WFM_ITEM.pack(
        channel, 0, 0, 0, 0, 0, 10.0, vert_div, 0.0,
        len(samples), data_off, *([0] * 16),
    )
    pad_items = b"".join(
        _WFM_ITEM.pack(ch, 0, 0, 0, 0, 0, 1.0, 1.0e-3, 0.0,
                       0, data_off, *([0] * 16))
        for ch in range(2, n_items + 1)
    )
    return head + item + pad_items + samples


# ---------------------------------------------------------------------------
# Identification / snapshot
# ---------------------------------------------------------------------------


def test_idn_and_query_raw_passthrough() -> None:
    drv, t = _make_driver()
    assert drv.idn() == "ZHIYUANELECT,ZDS1104,SN0001,V1.0,1.0.0"
    assert drv.query_raw(":SYSTem:VERSion?") == "1999.0"
    drv.query_raw(":RUN")
    assert ":RUN" in t.writes


def test_active_channel_count_uses_zds_nodes() -> None:
    drv, t = _make_driver(response_map={
        ":CHANnel1:DISPlay?": "1",
        ":CHANnel2:DISPlay?": "0",
        ":CHANnel3:DISPlay?": "0",
        ":CHANnel4:DISPlay?": "1",
    })
    assert drv.active_channel_count() == 2
    for ch in (1, 2, 3, 4):
        assert f":CHANnel{ch}:DISPlay?" in t.queries


def test_timebase_uses_zds_nodes() -> None:
    drv, t = _make_driver()
    assert drv.timebase_s_per_div() == pytest.approx(1.0e-3)
    assert ":TIMebase:SCALe?" in t.queries


# ---------------------------------------------------------------------------
# Channel setup
# ---------------------------------------------------------------------------


def test_set_channel_sends_zds_nodes_and_probe_literal() -> None:
    drv, t = _make_driver()
    drv.set_channel(ChannelSetup(
        channel=1, probe=10, coupling="DC", scale_v_per_div=0.5,
        bw_limit="20M", display=True,
    ))
    # Probe ratio must be an integer literal (10, not 10.0) and land
    # before scale (it re-ranges scale/offset).
    assert ":CHANnel1:PROBe 10" in t.writes
    assert not any(":CHANnel1:PROBe 10.0" == w for w in t.writes)
    assert t.writes.index(":CHANnel1:PROBe 10") < t.writes.index(":CHANnel1:SCALe 0.5")
    assert ":CHANnel1:COUPling DC" in t.writes
    assert ":CHANnel1:BWLimit 20M" in t.writes
    assert ":CHANnel1:DISPlay ON" in t.writes


def test_set_channel_fractional_probe_uses_g_format() -> None:
    drv, t = _make_driver()
    drv.set_channel(ChannelSetup(channel=2, probe=0.5))
    assert ":CHANnel2:PROBe 0.5" in t.writes


def test_set_channel_invalid_coupling_rejects_before_any_write() -> None:
    drv, t = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="coupling"):
        drv.set_channel(ChannelSetup(channel=1, coupling="FOO", scale_v_per_div=1.0))
    assert t.writes == []


def test_get_channel_parses_all_nodes() -> None:
    drv, _ = _make_driver()
    state = drv.get_channel(1)
    assert state.channel == 1
    assert state.scale_v_per_div == pytest.approx(1.0e-3)
    assert state.coupling == "DC"
    assert state.display is True
    assert state.probe == pytest.approx(10.0)
    assert state.bw_limit == "OFF"
    assert state.units == "VOLTage"


# ---------------------------------------------------------------------------
# Timebase / acquire
# ---------------------------------------------------------------------------


def test_set_timebase_has_no_main_level() -> None:
    drv, t = _make_driver()
    drv.set_timebase(TimebaseSetup(mode="MAIN", s_per_div=1.0e-3))
    assert ":TIMebase:MODE MAIN" in t.writes
    assert ":TIMebase:SCALe 0.001" in t.writes
    assert not any(":MAIN:" in w for w in t.writes)


def test_set_acquire_sends_zds_nodes() -> None:
    drv, t = _make_driver(response_map={":CHANnel1:DISPlay?": "1"})
    drv.set_acquire(AcquireSetup(type="AVERages", averages=128,
                                 memory_depth=1400000))
    assert ":ACQuire:TYPE AVERages" in t.writes
    assert ":ACQuire:AVERages 128" in t.writes
    assert ":ACQuire:MDEPth 1400000" in t.writes


def test_set_acquire_rejects_off_ladder_depth() -> None:
    drv, t = _make_driver()
    with pytest.raises(AcquisitionValidationError, match="memory_depth"):
        drv.set_acquire(AcquireSetup(memory_depth=999))
    assert t.writes == []


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


def test_set_trigger_edge_round_trip_uses_ch_sources() -> None:
    drv, t = _make_driver(response_map={
        ":TRIGger:MODE?": "EDGE",
        ":TRIGger:SWEep?": "AUTO",
        ":TRIGger:COUPling?": "DC",
        ":TRIGger:HOLDoff?": "0",
        ":GLOBal:RUN:STATe?": "Stop",
        ":TRIGger:EDGE:SOURce?": "CH1",
        ":TRIGger:EDGE:SLOPe?": "NEGative",
        ":TRIGger:EDGE:LEVel?": "0.0",
    })
    state = drv.set_trigger(TriggerSetup(
        mode="EDGE", sweep="AUTO",
        params={"source": 1, "slope": "NEG"},
    ))
    assert ":TRIGger:MODE EDGE" in t.writes
    assert ":TRIGger:SWEep AUTO" in t.writes
    # ZDS trigger sources are CH<n>, not CHAN<n>.
    assert ":TRIGger:EDGE:SOURce CH1" in t.writes
    assert ":TRIGger:EDGE:SLOPe NEGative" in t.writes
    assert state.params["source"] == "CH1"
    assert state.status == "Stop"


def test_set_trigger_rejects_single_sweep() -> None:
    drv, t = _make_driver()
    with pytest.raises(TriggerValidationError, match="sweep"):
        drv.set_trigger(TriggerSetup(mode="EDGE", sweep="SINGLE"))
    assert t.writes == []


def test_run_control_maps_zds_actions() -> None:
    drv, t = _make_driver()
    assert drv.run_control("STOP") == "STOP"
    assert ":STOP" in t.writes
    assert drv.run_control("single") == "SINGLE"
    assert ":SINGle" in t.writes
    with pytest.raises(TriggerValidationError, match="run action"):
        drv.run_control("FORCE")  # no SCPI equivalent registered (yet)


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def test_measure_sends_per_item_queries() -> None:
    drv, t = _make_driver(response_map={
        ":MEASure:VPP? CHANnel1": "2.000000E+0",
        ":MEASure:FREQuency? CHANnel1": "1.000000E+3",
    })
    out = drv.measure(["VPP", "FREQUENCY"], source="CHAN1")
    assert ":MEASure:VPP? CHANnel1" in t.queries
    assert ":MEASure:FREQuency? CHANnel1" in t.queries
    assert out == {"VPP": pytest.approx(2.0), "FREQUENCY": pytest.approx(1000.0)}


def test_measure_dual_source_uses_second_source() -> None:
    drv, t = _make_driver(response_map={
        ":MEASure:RRDelay? CHANnel1,CHANnel2": "1.000000E-6",
    })
    out = drv.measure(["RDELAY"], source="CHAN1", source2="2")
    assert ":MEASure:RRDelay? CHANnel1,CHANnel2" in t.queries
    assert out["RDELAY"] == pytest.approx(1.0e-6)


def test_measure_dual_source_without_source2_rejects() -> None:
    drv, t = _make_driver()
    with pytest.raises(MeasureValidationError, match="second source"):
        drv.measure(["RDELAY"], source="CHAN1")
    assert t.queries == []


def test_measure_statistics_enables_item_then_queries_stats() -> None:
    drv, t = _make_driver(response_map={
        ":MEASure:VPP:CURRent? CHANnel1": "2.000000E+0",
        ":MEASure:VPP:MAXImum? CHANnel1": "2.500000E+0",
        ":MEASure:VPP:MINImum? CHANnel1": "1.800000E+0",
        ":MEASure:VPP:AVERage? CHANnel1": "2.100000E+0",
        ":MEASure:VPP:DEViation? CHANnel1": "1.000000E-3",
        ":MEASure:VPP:COUNt? CHANnel1": "42",
    })
    results = drv.measure_statistics(["VPP"], source="CHAN1")
    # Item enabled first so statistics accumulate.
    assert t.writes == [":MEASure:VPP CHANnel1"]
    for st in ("CURRent", "MAXImum", "MINImum", "AVERage", "DEViation", "COUNt"):
        assert f":MEASure:VPP:{st}? CHANnel1" in t.queries
    r = results[0]
    assert r.current == pytest.approx(2.0)
    assert r.maximum == pytest.approx(2.5)
    assert r.count == 42


# ---------------------------------------------------------------------------
# Waveform (WFM stream)
# ---------------------------------------------------------------------------


def test_read_waveform_screen_queries_multiwave() -> None:
    payload = _wfm_payload(samples=b"\x80\x96", sample_rate=1.0e9)
    drv, t = _make_driver(lp_binary_response=payload)
    wf = drv.read_waveform("1")
    assert ":GLOBal:MULTiwave? SCREen,CHANnel1" in t.queries
    assert wf.source == "CHAN1"  # canonical tool-layer spelling
    assert wf.n_samples == 2
    assert wf.dt_s == pytest.approx(1.0e-9)
    # Verified encoding: code 0x80 (128) = screen center → 0 V at offset 0.
    assert wf.volts[0] == pytest.approx(0.0, abs=1e-12)
    # Codes are screen-referred: 0x96 (150) sits 22 codes BELOW center,
    # i.e. on the negative-volt side → −22/25 × 1 mV.
    assert wf.volts[1] == pytest.approx(-22.0 / 25.0 * 1.0e-3)


def test_read_waveform_screen_retries_empty_block_transient() -> None:
    """MULTiwave? returns 0-length until the first complete acquisition
    exists after a config change — the driver retries instead of
    surfacing the transient."""
    payload = _wfm_payload(samples=b"\x80")
    drv, t = _make_driver(lp_binary_response=payload)
    drv._wfm_empty_retry_delay_s = 0  # keep the test instant

    calls = {"n": 0}
    real = t.query_length_prefixed

    def flaky(cmd: str, recv_max: int = 64 * 1024 * 1024) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ScpiEmptyBlockError("non-positive length 0")
        return real(cmd, recv_max)

    t.query_length_prefixed = flaky  # type: ignore[method-assign]
    wf = drv.read_waveform("1")
    assert calls["n"] == 2
    assert wf.n_samples == 1


def test_read_waveform_raw_requires_stop_and_stretches_timeout() -> None:
    payload = _wfm_payload(samples=b"\x80")
    drv, t = _make_driver(
        response_map={":GLOBal:RUN:STATe?": "Run"},
        lp_binary_response=payload,
    )
    drv._state_poll_deadline_s = 0  # fail fast on "still running"
    with pytest.raises(RuntimeError, match="must be stopped"):
        drv.read_waveform("1", mode="RAW")

    drv2, t2 = _make_driver(
        response_map={":GLOBal:RUN:STATe?": "Stop"},
        lp_binary_response=_wfm_payload(channel=2, samples=b"\x80"),
    )
    wf = drv2.read_waveform("CHAN2", mode="RAW")
    assert ":GLOBal:MULTiwave? MEMOry,CHANnel2" in t2.queries
    assert wf.n_samples == 1
    # timeout stretched for the big transfer and restored afterwards
    assert t2.timeout_s == 3.0


def test_read_waveform_rejects_unknown_mode() -> None:
    drv, _ = _make_driver()
    with pytest.raises(ValueError, match="unsupported waveform mode"):
        drv.read_waveform("1", mode="MAX")


def test_parse_wfm_accepts_zero_based_channel_numbering() -> None:
    payload = _wfm_payload(channel=0, samples=b"\x80")
    head, item, data = _parse_wfm(payload, 1)
    assert item["vert_div_v"] == pytest.approx(1.0e-3)
    assert data == b"\x80"
    assert head["sample_rate"] == pytest.approx(1.0e9)


def test_parse_wfm_rejects_bad_magic() -> None:
    payload = _wfm_payload()
    payload = b"XXX\x00" + payload[4:]
    with pytest.raises(ScpiError, match="magic"):
        _parse_wfm(payload, 1)


def test_parse_wfm_rejects_truncated_stream() -> None:
    with pytest.raises(ScpiError, match="too short"):
        _parse_wfm(b"WFM\x00" + b"\x00" * 8, 1)


def test_parse_wfm_rejects_missing_channel() -> None:
    payload = _wfm_payload(channel=3, samples=b"\x80")
    with pytest.raises(ScpiError, match="no item for channel 1"):
        _parse_wfm(payload, 1)


def test_parse_wfm_rejects_out_of_range_data_window() -> None:
    samples = b"\x80\x80"
    # Rebuild the item with iDataLength=1000 starting after the header —
    # far past the end of the 2-sample stream.
    bad_item = _WFM_ITEM.pack(
        1, 0, 0, 0, 0, 0, 10.0, 1.0e-3, 0.0,
        1000, _WFM_HEAD.size + _WFM_ITEM.size, *([0] * 16),
    )
    payload = _wfm_payload()[:_WFM_HEAD.size] + bad_item + samples
    with pytest.raises(ScpiError, match="data window"):
        _parse_wfm(payload, 1)


# ---------------------------------------------------------------------------
# Screenshot + cursors
# ---------------------------------------------------------------------------


def test_screenshot_falls_back_to_bmp() -> None:
    drv, t = _make_driver()
    result = drv.screenshot(ScreenshotPlan(image_format="PNG"))
    assert result.image_format == "BMP"
    assert result.image_bytes.startswith(b"BM")
    assert ":DISPlay:DATA?" in t.queries
    # No channel-label SCPI exists on ZDS1000 — nothing must be sent.
    assert t.writes == []


def test_screenshot_cursor_pair_uses_manual_pixel_formulas() -> None:
    drv, t = _make_driver(response_map={
        ":TIMebase:SCALe?": "1.000000E-3",
        ":TIMebase:OFFSet?": "0.000000E+0",
        ":CHANnel1:SCALe?": "1.000000E+0",
        ":CHANnel1:OFFSet?": "0.000000E+0",
    })
    result = drv.screenshot(ScreenshotPlan(cursor_pairs=[
        CursorPair(label="half", source_channel=1, ax_t_s=0.0, ay_v=0.0),
    ]))
    assert ":CURSor:MODE ALL" in t.writes  # both t and v → ALL
    # t=0, offset=0 → x = 350 + 0 = 350; u=0 → y = 200.
    assert ":CURSor:X1Position 350" in t.writes
    assert ":CURSor:Y1Position 200" in t.writes
    assert result.cursors_set[0].ax_t_s == 0.0


def test_screenshot_time_only_cursor_uses_vertical_mode() -> None:
    drv, t = _make_driver(response_map={
        ":TIMebase:SCALe?": "1.000000E-3",
        ":TIMebase:OFFSet?": "0.000000E+0",
    })
    drv.screenshot(ScreenshotPlan(cursor_pairs=[
        CursorPair(label="t1", source_channel=1, ax_t_s=2.5e-3),
    ]))
    assert ":CURSor:MODE VERTical" in t.writes
    # +2.5 ms at 1 ms/div → 350 + 2.5 × 50 = 475.
    assert ":CURSor:X1Position 475" in t.writes


def test_screenshot_cursor_positions_are_clamped() -> None:
    drv, t = _make_driver(response_map={
        ":TIMebase:SCALe?": "1.000000E-3",
        ":TIMebase:OFFSet?": "0.000000E+0",
    })
    drv.screenshot(ScreenshotPlan(cursor_pairs=[
        CursorPair(label="far", source_channel=1, ax_t_s=1.0),
    ]))
    assert ":CURSor:X1Position 699" in t.writes
