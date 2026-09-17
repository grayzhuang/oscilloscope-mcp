"""ZLG ZDS1000-series oscilloscope driver (ZDS1104, …).

SCPI dialect reference: ZLG ZDS1000 Series Programming Manual
UM01010101 V1.03 (distilled in ``docs/references/ZLG/ZDS1000_pm.md``).
The series speaks raw TCP port 5025 with line-based SCPI commands.
Screenshots (``:DISPlay:DATA?``) come back as an IEEE ``#9`` block, but
waveform reads (``:GLOBal:MULTiwave?``) use a 4-byte little-endian
int32 length prefix followed by a self-describing ``WFM`` binary
stream — the scaling factors live inside that stream instead of a
``:WAV:PREamble?`` CSV.

Vendor quirks handled here (vs the RIGOL DS1000Z driver):

- Channel nodes are spelled long (``:CHANnel<n>:BWLimit`` / ``:UNITs``
  / ``:PROBe``), and probe ratios must be integer literals (``10`` not
  ``10.0``) per manual §7.
- Timebase has no ``:MAIN:`` level: ``:TIMebase:SCALe``.
- ``SINGLE`` is a run-control action, not a sweep mode.
- Run state comes from ``:GLOBal:RUN:STATe?`` → Run/Single/Stop.
- Trigger sources are ``CH<n>``; measurement / waveform sources are
  ``CHANnel<n>``.
- Screenshots are BMP only (``:DISPlay:DATA?`` takes no format
  parameter); a PNG request falls back to BMP and the applied format is
  echoed in the result.

WFM binary layout (manual §10): ``Head + Item[n] + Data[n]`` with
``sizeof(wfm_head_info)=424`` / ``sizeof(wfm_item_info)=120`` under x86
natural alignment, little-endian — **verified on a real ZDS1104**
(firmware 1.2.67): the parser cross-checks the ``cFileType == "WFM"``
magic and a sane ``iItemNum`` anyway, so a firmware layout change
surfaces loudly instead of producing shifted garbage. Sample encoding
(verified the same way, via a GND-coupling offset sweep): unsigned
8-bit, code 128 = screen center, 25 codes per vertical division, codes
are *screen-referred* — positive volts sit further up the screen and
therefore at *smaller* codes:

    volts = (128 − code) / 25 × vert_div + vert_offset

Read-mode semantics (verified on hardware):

- ``SCREen`` returns the **full acquisition memory** (``iDataLength``
  == the configured memory depth, not a ~1200-point screen
  decimation like RIGOL NORMal) and is readable while running.
- ``MEMOry`` returns 0 bytes while running — the scope must be
  :STOPped first (the RAW path enforces this).
- ``:ACQuire:SRATe?`` is coupled to timebase × memory depth and caps
  at 1 GSa/s; enabling 2/4 channels derates it to 500 / 250 MSa/s.
"""

from __future__ import annotations

import re
import struct
import time
from typing import Any

from oscilloscope_mcp.helpers import acquisition as acq_helper
from oscilloscope_mcp.helpers import measure as meas_helper
from oscilloscope_mcp.helpers import trigger as trig_helper
from oscilloscope_mcp.instruments._base import (
    AcquireSetup,
    AcquireState,
    ChannelSetup,
    ChannelState,
    CursorPair,
    MeasureStatResult,
    Scope,
    ScreenshotPlan,
    ScreenshotResult,
    TimebaseSetup,
    TimebaseState,
    TriggerSetup,
    TriggerState,
    Waveform,
)
from oscilloscope_mcp.transport.scpi_lan import (
    ScpiEmptyBlockError,
    ScpiError,
    ScpiLan,
)


# Waveform-area geometry for cursor pixel translation (manual §8: the
# grid is 14 × 8 divisions at 50 px/div, centered at 350/200 — unlike
# the DS1000Z's 12 × 8 grid at 800 × 480).
_PX_PER_TIME_DIV = 50
_PX_PER_VOLT_DIV = 50
_TIME_CENTER_PX = 350
_VOLT_CENTER_PX = 200


# --- WFM stream structs (manual §10) ---------------------------------------
#
# Field order per wfm_head_info / wfm_item_info; little-endian with
# x86 natural alignment (no implicit padding needed at these offsets —
# verified by summing the field sizes to the documented 424 / 120 B).
_WFM_HEAD = struct.Struct(
    "<"                  # little-endian, no native padding
    "4s64s128s40s"       # cFileType, cDevName, cFirmwareVersion, cDataFormat
    "i"                  # iRev0
    "3d"                 # dfHrztDivision, dfHrztOffset, dfStartTime
    "2i"                 # iAcqMode, iRev1
    "d"                  # dfSampleRate
    "2i"                 # iTrigMode, iTrigSource
    "64s"                # cTrigType
    "i"                  # iItemNum
    "17i"                # iRev[17]
)
_WFM_ITEM = struct.Struct(
    "<"                  # little-endian, no native padding
    "6i"                 # iChannel, iCoupleMode, iBwLimit, iProbeType,
                         # iReversed, iRev0
    "3d"                 # dfProbeAtt, dfVertDivision, dfVertOffset
    "2i"                 # iDataLength, iDataOffset
    "16i"                # iRev1[16]
)

# --- Sample encoding (verified on a real ZDS1104, fw 1.2.67) ---------------
#
# Unsigned 8-bit codes, 128 = screen center, 25 codes per vertical
# division; codes are screen-referred, so positive volts (further up
# the screen) map to SMALLER codes:
#     volts = (128 - code) / 25 * vert_div + vert_offset
# Pinned down by a GND-coupling offset sweep: offsets -4.08/0/+2 V at
# 2 V/div gave codes 77/128/153 exactly.
_CODE_ZERO = 128.0
_CODES_PER_DIV = 25.0


class ZlgZds1000(Scope):
    """Driver for any ZDS1000-series scope (ZDS1104, …)."""

    transport: ScpiLan

    # :GLOBal:RUN:STATe? lags :RUN/:STOP/:SINGle by up to ~0.4 s on this
    # firmware (measured), so run_control POLLS for the expected state
    # instead of sleeping a fixed interval; tests set
    # run_status_settle_s to 0 to skip the poll entirely.
    run_status_settle_s: float = 0.3

    # Deadline for the run-control status poll (and the RAW-read stop
    # check); tests set this to 0 for fail-fast behaviour.
    _state_poll_deadline_s: float = 1.5

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------

    def idn(self) -> str:
        return self.transport.query("*IDN?")

    def query_raw(self, scpi: str) -> str:
        if "?" in scpi:
            return self.transport.query(scpi)
        self.transport.write(scpi)
        return ""

    # ------------------------------------------------------------------
    # State snapshot
    # ------------------------------------------------------------------

    def active_channel_count(self) -> int:
        ch_total = int(self.profile.get("capability", {}).get("channels", 4))
        count = 0
        for ch in range(1, ch_total + 1):
            on = self.transport.query(f":CHANnel{ch}:DISPlay?").strip()
            if on in ("1", "ON"):
                count += 1
        return count

    def timebase_s_per_div(self) -> float:
        return float(self.transport.query(":TIMebase:SCALe?"))

    def channel_scale_v_per_div(self, channel: int) -> float:
        return float(self.transport.query(f":CHANnel{channel}:SCALe?"))

    def channel_offset_v(self, channel: int) -> float:
        return float(self.transport.query(f":CHANnel{channel}:OFFSet?"))

    # ------------------------------------------------------------------
    # Acquisition setup — vertical (channel) + horizontal (timebase)
    # ------------------------------------------------------------------

    def get_channel(self, channel: int) -> ChannelState:
        n = acq_helper.validate_channel_number(self.profile, channel)
        return ChannelState(
            channel=n,
            scale_v_per_div=_parse_float(self.transport.query(f":CHANnel{n}:SCALe?")),
            offset_v=_parse_float(self.transport.query(f":CHANnel{n}:OFFSet?")),
            coupling=self.transport.query(f":CHANnel{n}:COUPling?").strip() or None,
            display=_parse_bool(self.transport.query(f":CHANnel{n}:DISPlay?")),
            probe=_parse_float(self.transport.query(f":CHANnel{n}:PROBe?")),
            bw_limit=self.transport.query(f":CHANnel{n}:BWLimit?").strip() or None,
            invert=_parse_bool(self.transport.query(f":CHANnel{n}:INVert?")),
            units=self.transport.query(f":CHANnel{n}:UNITs?").strip() or None,
        )

    def set_channel(self, setup: ChannelSetup) -> ChannelState:
        # Validate the channel number + every requested field up front
        # (raises before any SCPI is sent), then build a single burst.
        n = acq_helper.validate_channel_number(self.profile, setup.channel)
        cmds: list[str] = []
        # Probe FIRST: changing the attenuation ratio re-ranges scale /
        # offset on the instrument, so it must land before they are set.
        if setup.probe is not None:
            ratio = acq_helper.validate_probe(self.profile, setup.probe)
            cmds.append(f":CHANnel{n}:PROBe {_format_probe_literal(ratio)}")
        if setup.coupling is not None:
            cmds.append(f":CHANnel{n}:COUPling {acq_helper.validate_coupling(self.profile, setup.coupling)}")
        if setup.units is not None:
            cmds.append(f":CHANnel{n}:UNITs {acq_helper.validate_units(self.profile, setup.units)}")
        if setup.scale_v_per_div is not None:
            scale = acq_helper.validate_channel_scale(self.profile, setup.scale_v_per_div)
            cmds.append(f":CHANnel{n}:SCALe {scale:g}")
        if setup.offset_v is not None:
            offset = acq_helper.validate_channel_offset(self.profile, setup.offset_v)
            cmds.append(f":CHANnel{n}:OFFSet {offset:g}")
        if setup.bw_limit is not None:
            cmds.append(f":CHANnel{n}:BWLimit {acq_helper.validate_bw_limit(self.profile, setup.bw_limit)}")
        if setup.invert is not None:
            cmds.append(f":CHANnel{n}:INVert {_on_off(acq_helper.validate_invert(setup.invert))}")
        if setup.display is not None:
            cmds.append(f":CHANnel{n}:DISPlay {_on_off(acq_helper.validate_display(setup.display))}")
        self.transport.write_many(cmds)
        return self.get_channel(n)

    def get_timebase(self) -> TimebaseState:
        return TimebaseState(
            s_per_div=_parse_float(self.transport.query(":TIMebase:SCALe?")),
            offset_s=_parse_float(self.transport.query(":TIMebase:OFFSet?")),
            mode=self.transport.query(":TIMebase:MODE?").strip() or None,
        )

    def set_timebase(self, setup: TimebaseSetup) -> TimebaseState:
        # Validate every requested field up front (raises before any SCPI).
        cmds: list[str] = []
        # Mode FIRST (MAIN/XY/ROLL) so scale/offset apply to the
        # resulting main-window state.
        if setup.mode is not None:
            cmds.append(f":TIMebase:MODE {acq_helper.validate_timebase_mode(self.profile, setup.mode)}")
        if setup.s_per_div is not None:
            scale = acq_helper.validate_timebase_scale(self.profile, setup.s_per_div)
            cmds.append(f":TIMebase:SCALe {scale:g}")
        if setup.offset_s is not None:
            offset = acq_helper.validate_timebase_offset(self.profile, setup.offset_s)
            cmds.append(f":TIMebase:OFFSet {offset:g}")
        self.transport.write_many(cmds)
        return self.get_timebase()

    # ------------------------------------------------------------------
    # Acquisition mode (acquire type / averages / memory depth)
    # ------------------------------------------------------------------

    def get_acquire(self) -> AcquireState:
        acq_type = self.transport.query(":ACQuire:TYPE?").strip() or None
        averages = _parse_int(self.transport.query(":ACQuire:AVERages?"))
        mdepth_raw = self.transport.query(":ACQuire:MDEPth?").strip()
        if mdepth_raw.upper() == "AUTO":
            memory_depth: str | int | None = "AUTO"
        else:
            memory_depth = _parse_int(mdepth_raw)
        sample_rate = _parse_float(self.transport.query(":ACQuire:SRATe?"))
        return AcquireState(
            type=acq_type,
            averages=averages,
            memory_depth=memory_depth,
            sample_rate=sample_rate,
        )

    def set_acquire(self, setup: AcquireSetup) -> AcquireState:
        # Validate every requested field up front (raises before any SCPI).
        cmds: list[str] = []
        if setup.type is not None:
            validated_type = acq_helper.validate_acquire_type(self.profile, setup.type)
            cmds.append(f":ACQuire:TYPE {validated_type}")
        if setup.averages is not None:
            validated_avg = acq_helper.validate_averages(self.profile, setup.averages)
            cmds.append(f":ACQuire:AVERages {validated_avg}")
        if setup.memory_depth is not None:
            active = self.active_channel_count()
            validated_mdepth = acq_helper.validate_memory_depth(
                self.profile, active, setup.memory_depth
            )
            cmds.append(f":ACQuire:MDEPth {validated_mdepth}")
        self.transport.write_many(cmds)
        return self.get_acquire()

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------

    def get_trigger(self) -> TriggerState:
        mode = trig_helper.keyword_from_query(
            self.profile, self.transport.query(":TRIGger:MODE?")
        )
        state = TriggerState(
            mode=mode,
            status=self.transport.query(":GLOBal:RUN:STATe?").strip() or None,
            sweep=self.transport.query(":TRIGger:SWEep?").strip() or None,
            coupling=self.transport.query(":TRIGger:COUPling?").strip() or None,
            holdoff_s=_parse_float(self.transport.query(":TRIGger:HOLDoff?")),
        )
        # Read every readable parameter the current mode declares in its
        # profile schema — fully generic across all trigger types. A
        # param that can't be read is skipped rather than failing the
        # whole read.
        entry = trig_helper.normalize_trigger_keyword(self.profile, mode)
        for name, (query, spec) in trig_helper.param_read_queries(entry).items():
            try:
                raw = self.transport.query(query)
            except ScpiError:
                continue
            value = trig_helper.parse_param_value(spec, raw)
            if value is not None:
                state.params[name] = value
        return state

    def set_trigger(self, setup: TriggerSetup) -> TriggerState:
        # Resolve the effective mode (target if given, else current) so
        # mode-specific params validate against the right schema.
        if setup.mode is not None:
            entry = trig_helper.normalize_trigger_keyword(self.profile, setup.mode)
        else:
            entry = trig_helper.normalize_trigger_keyword(
                self.profile,
                trig_helper.keyword_from_query(
                    self.profile, self.transport.query(":TRIGger:MODE?")
                ),
            )

        # Validate ALL params up front (raises before any SCPI is sent).
        param_writes = trig_helper.build_param_writes(
            self.profile, entry, setup.params or {}
        )
        sweep = (
            trig_helper.validate_sweep(self.profile, setup.sweep)
            if setup.sweep is not None else None
        )
        coupling = (
            trig_helper.validate_coupling(self.profile, setup.coupling)
            if setup.coupling is not None else None
        )

        # Send the whole burst on one connection (mode FIRST so the
        # type-specific writes target it), synced with *OPC?.
        cmds: list[str] = []
        if setup.mode is not None:
            cmds.append(f":TRIGger:MODE {entry['keyword']}")
        if sweep is not None:
            cmds.append(f":TRIGger:SWEep {sweep}")
        if coupling is not None:
            cmds.append(f":TRIGger:COUPling {coupling}")
        if setup.holdoff_s is not None:
            cmds.append(f":TRIGger:HOLDoff {setup.holdoff_s:g}")
        cmds.extend(f"{scpi_node} {value}" for scpi_node, value in param_writes)
        self.transport.write_many(cmds)

        return self.get_trigger()

    def run_control(self, action: str) -> str:
        name, scpi = trig_helper.resolve_run_action(self.profile, action)
        self.transport.write(scpi)
        if self.run_status_settle_s:
            self._await_run_state(name)
        return name

    # Acceptable :GLOBal:RUN:STATe? values per action — SINGLE may have
    # already completed (→ Stop) by the time the poll runs.
    _EXPECTED_RUN_STATE = {
        "RUN": {"RUN"},
        "STOP": {"STOP"},
        "SINGLE": {"SINGLE", "STOP"},
    }

    def _await_run_state(self, action: str) -> None:
        """Poll :GLOBal:RUN:STATe? until it reflects ``action`` (or the
        deadline expires — a timeout is not an error here, the caller's
        next status read will surface whatever the scope reports)."""
        expected = self._EXPECTED_RUN_STATE.get(action)
        if not expected:
            time.sleep(self.run_status_settle_s)
            return
        deadline = time.monotonic() + self._state_poll_deadline_s
        while True:
            state = self.transport.query(":GLOBal:RUN:STATe?").strip().upper()
            if state in expected or time.monotonic() >= deadline:
                return
            time.sleep(0.1)

    # ------------------------------------------------------------------
    # Automatic measurements
    # ------------------------------------------------------------------

    def measure(
        self,
        items: list[str],
        source: str = "CHAN1",
        source2: str | None = None,
    ) -> dict[str, float | None]:
        # Validate + normalize EVERYTHING up front (raises before any
        # SCPI is sent). normalize_source yields CHANnel<n> — the
        # spelling :MEASure:<ITEM>? expects.
        resolved = meas_helper.resolve_items(self.profile, items)
        src = meas_helper.normalize_source(self.profile, source)
        src2 = (
            meas_helper.normalize_source(self.profile, source2)
            if source2 is not None else None
        )
        # A dual-source item without source2 is a caller error — reject
        # it rather than send a malformed :MEASure:<ITEM>? query.
        if src2 is None:
            for name, spec in resolved:
                if meas_helper.is_dual_source(spec):
                    raise meas_helper.MeasureValidationError(
                        f"measurement item {name!r} requires a second source "
                        "(source2) — it measures between two channels"
                    )

        out: dict[str, float | None] = {}
        for name, spec in resolved:
            kw = spec["scpi"]
            if meas_helper.is_dual_source(spec):
                cmd = f":MEASure:{kw}? {src},{src2}"
            else:
                cmd = f":MEASure:{kw}? {src}"
            try:
                raw = self.transport.query(cmd)
            except ScpiError:
                # A firmware quirk on one item must not fail the whole
                # batch; record it as unmeasurable and continue.
                out[name] = None
                continue
            out[name] = meas_helper.parse_measure_value(raw)
        return out

    # ------------------------------------------------------------------
    # Measurement statistics (:MEASure:<ITEM>:<STAT>?)
    # ------------------------------------------------------------------

    def measure_statistics(
        self,
        items: list[str],
        source: str = "CHAN1",
        stat_types: list[str] | None = None,
    ) -> list[MeasureStatResult]:
        # Validate + normalize everything up front (mirrors measure()).
        resolved = meas_helper.resolve_items(self.profile, items)
        src = meas_helper.normalize_source(self.profile, source)

        # Resolve stat_types: default to all six from the profile.
        if stat_types is None:
            all_types = meas_helper.stat_types_list(self.profile)
        else:
            all_types = [
                meas_helper.validate_stat_type(self.profile, st)
                for st in stat_types
            ]

        # Map canonical stat-type (ZDS spelling) → MeasureStatResult field.
        _STAT_FIELD = {
            "CURRent": "current",
            "MAXImum": "maximum",
            "MINImum": "minimum",
            "AVERage": "average",
            "DEViation": "deviation",
            "COUNt": "count",
        }

        results: list[MeasureStatResult] = []
        for name, spec in resolved:
            kw = spec["scpi"]
            # Enable the item first so the instrument accumulates data
            # (ZDS statistics only run for on-screen measurement items).
            self.transport.write(f":MEASure:{kw} {src}")
            result = MeasureStatResult(item=name, source=src)
            for st in all_types:
                cmd = f":MEASure:{kw}:{st}? {src}"
                try:
                    raw = self.transport.query(cmd)
                except ScpiError:
                    continue
                field = _STAT_FIELD.get(st)
                if field is None:
                    continue
                if field == "count":
                    # COUNt returns an integer (or sentinel).
                    parsed = meas_helper.parse_measure_value(raw)
                    setattr(result, field, int(parsed) if parsed is not None else None)
                else:
                    setattr(result, field, meas_helper.parse_measure_value(raw))
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Waveform capture
    # ------------------------------------------------------------------

    def read_waveform(self, source: str, mode: str = "NORMal") -> Waveform:
        """Read CH<n> waveform in the specified mode.

        ``mode`` must be ``"NORMal"`` (``MULTiwave? SCREen``) or
        ``"RAW"`` (``MULTiwave? MEMOry``). On ZDS1000 firmware both
        return the full acquisition memory (``iDataLength`` == the
        configured memory depth, up to 28 M points) — unlike RIGOL,
        NORMal is NOT a ~1200-point screen decimation. The difference
        is the run-state requirement: SCREen is readable while running,
        MEMOry returns 0 bytes unless the scope is stopped (raises
        :class:`RuntimeError`).
        """
        mode_upper = mode.strip().upper()
        if mode_upper == "RAW":
            return self._read_waveform_memory(source)
        if mode_upper == "NORMAL":
            return self._read_waveform_screen(source)
        raise ValueError(
            f"unsupported waveform mode {mode!r}; expected 'NORMal' or 'RAW'"
        )

    def _read_waveform_screen(self, source: str) -> Waveform:
        src = _normalize_source(source, self._channel_count())
        cmd = f":GLOBal:MULTiwave? SCREen,{_scpi_source(src)}"
        return self._waveform_from_wfm(self._query_wfm(cmd), src)

    # MULTiwave? returns 0 bytes until the first complete acquisition
    # exists after a run-state / config change (observed on hardware) —
    # retry the empty-block transient briefly instead of surfacing it.
    _wfm_empty_retries: int = 3
    _wfm_empty_retry_delay_s: float = 0.5

    def _query_wfm(self, cmd: str) -> bytes:
        for attempt in range(self._wfm_empty_retries + 1):
            if attempt:
                time.sleep(self._wfm_empty_retry_delay_s)
            try:
                return self.transport.query_length_prefixed(cmd)
            except ScpiEmptyBlockError:
                if attempt == self._wfm_empty_retries:
                    raise
                continue
        raise AssertionError("unreachable")  # pragma: no cover

    def _read_waveform_memory(self, source: str) -> Waveform:
        # MEMOry returns 0 bytes while the scope is running (verified on
        # hardware) — require a stopped acquisition like the DS1000Z RAW
        # path does. :GLOBal:RUN:STATe? lags :STOP by up to ~0.4 s on
        # this firmware (measured), so poll briefly before declaring the
        # scope still running.
        deadline = time.monotonic() + self._state_poll_deadline_s
        state = ""
        while True:
            state = self.transport.query(":GLOBal:RUN:STATe?").strip().upper()
            if state == "STOP" or time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        if state != "STOP":
            raise RuntimeError(
                f"scope must be stopped for RAW (MEMOry) waveform read "
                f"(state={state!r}); stop acquisition first with "
                "scope_trigger(action='STOP') or use SINGLE sweep"
            )
        src = _normalize_source(source, self._channel_count())
        # 28 Mpts can take tens of seconds to transfer — stretch the
        # socket timeout for the duration of the read (restored after).
        old_timeout = self.transport.timeout_s
        self.transport.timeout_s = max(old_timeout, 30.0)
        try:
            payload = self.transport.query_length_prefixed(
                f":GLOBal:MULTiwave? MEMOry,{_scpi_source(src)}"
            )
        finally:
            self.transport.timeout_s = old_timeout
        return self._waveform_from_wfm(payload, src)

    def _waveform_from_wfm(self, payload: bytes, src: str) -> Waveform:
        n = int(src.removeprefix("CHAN"))
        head, item, data = _parse_wfm(payload, n)
        volts = _codes_to_volts(data, item)
        dt_s = 1.0 / head["sample_rate"] if head["sample_rate"] > 0 else 0.0
        return Waveform(
            source=src, volts=volts,
            t0_s=head["start_time_s"], dt_s=dt_s,
        )

    def _channel_count(self) -> int:
        return int(self.profile.get("capability", {}).get("channels", 4))

    # ------------------------------------------------------------------
    # Screenshot + annotation
    # ------------------------------------------------------------------

    def screenshot(self, plan: ScreenshotPlan) -> ScreenshotResult:
        # 1. Channel labels — the ZDS1000 SCPI set has no channel-label
        #    command; report nothing applied rather than fabricate one.
        labels_applied: dict[int, str] = {}

        # 2. Cursors (manual §8 pixel formulas).
        cursors_applied: list[CursorPair] = []
        if plan.cursor_pairs:
            for pair in plan.cursor_pairs:
                applied = self._apply_cursor_pair(pair)
                cursors_applied.append(applied)

        # 3. Screenshot — :DISPlay:DATA? takes no format parameter and
        #    always returns a BMP (IEEE #9 block). A request for an
        #    unsupported format falls back to the profile's first
        #    supported one; the applied format is echoed in the result.
        fmt = (plan.image_format or "BMP").upper()
        supported = [
            str(f).upper()
            for f in self.profile.get("capability", {}).get(
                "screenshot_formats", []
            )
        ] or ["BMP"]
        if fmt not in supported:
            fmt = supported[0]
        payload = self.transport.query_binary(":DISPlay:DATA?")
        return ScreenshotResult(
            image_bytes=payload,
            image_format=fmt,
            cursors_set=cursors_applied,
            channel_labels_applied=labels_applied,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply_cursor_pair(self, pair: CursorPair) -> CursorPair:
        """Translate seconds/volts into cursor-position SCPI (manual §8:
        x = 350 + (t − t_offset)/t_div × 50, y = 200 − (u − u_offset)/u_div × 50).
        """
        has_t = pair.ax_t_s is not None or pair.bx_t_s is not None
        has_v = pair.ay_v is not None or pair.by_v is not None
        if not (has_t or has_v):
            return pair
        # Time cursors are vertical lines (VERTical mode); voltage
        # cursors are horizontal lines (HORIzontal); both → ALL.
        mode = "ALL" if has_t and has_v else ("VERTical" if has_t else "HORIzontal")
        self.transport.write(f":CURSor:MODE {mode}")
        if has_t:
            t_per_div = self.timebase_s_per_div()
            t_offset = _parse_float(self.transport.query(":TIMebase:OFFSet?"))
            if pair.ax_t_s is not None:
                px = _time_to_px(pair.ax_t_s, t_per_div, t_offset)
                self.transport.write(f":CURSor:X1Position {px}")
            if pair.bx_t_s is not None:
                px = _time_to_px(pair.bx_t_s, t_per_div, t_offset)
                self.transport.write(f":CURSor:X2Position {px}")
        if has_v:
            v_per_div = self.channel_scale_v_per_div(pair.source_channel)
            v_offset = self.channel_offset_v(pair.source_channel)
            if pair.ay_v is not None:
                py = _volt_to_px(pair.ay_v, v_per_div, v_offset)
                self.transport.write(f":CURSor:Y1Position {py}")
            if pair.by_v is not None:
                py = _volt_to_px(pair.by_v, v_per_div, v_offset)
                self.transport.write(f":CURSor:Y2Position {py}")
        return pair


# ---------------------------------------------------------------------------
# WFM stream parsing
# ---------------------------------------------------------------------------


def _parse_wfm(payload: bytes, channel: int) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """Parse a WFM stream (``Head + Item[n] + Data[n]``) and return
    ``(head_dict, item_dict, raw_sample_bytes)`` for ``channel``.

    Raises :class:`ScpiError` when the stream doesn't match the expected
    layout — the struct packing is a natural-alignment hypothesis (see
    module docstring), so parse failures should surface loudly rather
    than produce shifted garbage.
    """
    if len(payload) < _WFM_HEAD.size + _WFM_ITEM.size:
        raise ScpiError(
            f"WFM stream too short ({len(payload)} B < "
            f"{_WFM_HEAD.size + _WFM_ITEM.size} B header+item)"
        )
    (file_type, _dev, _fw, _fmt, _rev0, hrzt_div, hrzt_off, start_time,
     _acq_mode, _rev1, sample_rate, _trig_mode, _trig_src, _trig_type,
     item_num, *_rev) = _WFM_HEAD.unpack_from(payload, 0)
    if not file_type.startswith(b"WFM"):
        raise ScpiError(f"WFM cFileType magic mismatch: {file_type!r}")
    if not 1 <= item_num <= 4:
        raise ScpiError(f"WFM iItemNum out of range: {item_num}")
    head: dict[str, Any] = {
        "hrzt_div_s": hrzt_div,
        "hrzt_offset_s": hrzt_off,
        "start_time_s": start_time,
        "sample_rate": sample_rate,
        "item_num": item_num,
    }

    items_end = _WFM_HEAD.size + item_num * _WFM_ITEM.size
    if len(payload) < items_end:
        raise ScpiError(
            f"WFM stream truncated ({len(payload)} B < {items_end} B for "
            f"{item_num} item(s))"
        )
    by_channel: dict[int, Any] = {}
    for i in range(item_num):
        fields = _WFM_ITEM.unpack_from(payload, _WFM_HEAD.size + i * _WFM_ITEM.size)
        by_channel[fields[0]] = fields
    # iChannel numbering is not documented (0- or 1-based). A channel-0
    # key implies 0-based firmware; otherwise assume 1-based.
    if 0 in by_channel:
        item = by_channel.get(channel - 1)
    else:
        item = by_channel.get(channel)
    if item is None:
        raise ScpiError(
            f"WFM has no item for channel {channel} (found channels "
            f"{sorted(by_channel)})"
        )

    data_len, data_off = item[9], item[10]
    if data_len < 0 or data_off < 0 or data_off + data_len > len(payload):
        raise ScpiError(
            f"WFM item data window [{data_off}, {data_off + data_len}) "
            f"outside the {len(payload)} B stream"
        )
    item_dict: dict[str, Any] = {
        "probe_att": item[6],
        "vert_div_v": item[7],
        "vert_offset_v": item[8],
        "data_length": data_len,
    }
    return head, item_dict, payload[data_off:data_off + data_len]


def _codes_to_volts(data: bytes, item: dict[str, Any]) -> list[float]:
    """Convert raw sample codes to volts (verified encoding):

        volts = (128 − code) / 25 × vert_div + vert_offset

    Codes are screen-referred — a more positive voltage sits further
    up the screen and therefore at a smaller code (like pixel-y).
    """
    scale = item["vert_div_v"] / _CODES_PER_DIV
    offset = item["vert_offset_v"]
    return [(_CODE_ZERO - b) * scale + offset for b in data]


# ---------------------------------------------------------------------------
# Coordinate translation (manual §8 formulas)
# ---------------------------------------------------------------------------


def _time_to_px(t_s: float, t_per_div: float, t_offset_s: float | None) -> int:
    """x = 350 + (t_set − t_offset) / t_div × 50, clamped to 0..699."""
    if t_per_div <= 0:
        raise ValueError(f"timebase must be positive, got {t_per_div}")
    px = _TIME_CENTER_PX + (t_s - (t_offset_s or 0.0)) / t_per_div * _PX_PER_TIME_DIV
    return max(0, min(699, int(round(px))))


def _volt_to_px(v: float, v_per_div: float, v_offset_v: float | None) -> int:
    """y = 200 − (u_set − u_offset) / u_div × 50, clamped to 0..399.

    Positive volts go *up* the screen = *lower* pixel-y (origin
    top-left), hence the minus sign.
    """
    if v_per_div <= 0:
        raise ValueError(f"voltage scale must be positive, got {v_per_div}")
    px = _VOLT_CENTER_PX - (v - (v_offset_v or 0.0)) / v_per_div * _PX_PER_VOLT_DIV
    return max(0, min(399, int(round(px))))


# ---------------------------------------------------------------------------
# Small response parsers (same contracts as the DS1000Z driver's)
# ---------------------------------------------------------------------------


def _normalize_source(source: str, channel_count: int) -> str:
    """Normalize a source like ``"1"`` / ``"ch2"`` / ``"CHANnel3"`` to
    the canonical ``CHAN<n>`` form (the tool-layer convention used in
    :class:`Waveform`.source), bounded by the model's channel count.
    """
    s = str(source).strip().upper()
    m = re.fullmatch(r"(?:CH(?:AN(?:NEL)?)?)?([0-9]+)", s)
    if not m:
        raise ValueError(
            f"waveform source {source!r} invalid; expected "
            f"CHAN1..CHAN{channel_count}"
        )
    n = int(m.group(1))
    if not 1 <= n <= channel_count:
        raise ValueError(
            f"waveform source channel {n} out of range (1..{channel_count})"
        )
    return f"CHAN{n}"


def _scpi_source(src: str) -> str:
    """``CHAN<n>`` → ``CHANnel<n>`` — the long spelling the ZDS1000
    ``:GLOBal:MULTiwave?`` source arguments require."""
    return f"CHANnel{src.removeprefix('CHAN')}"


def _format_probe_literal(ratio: float) -> str:
    """ZDS1000 requires probe ratios written as integer literals when
    whole (``10`` not ``10.0``, manual §7); fractional ratios keep %g.
    """
    f = float(ratio)
    if f == int(f):
        return str(int(f))
    return f"{f:g}"


def _parse_float(resp: str) -> float | None:
    try:
        return float(resp.strip())
    except (ValueError, AttributeError):
        return None


def _parse_int(resp: str) -> int | None:
    try:
        return int(float(resp.strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_bool(resp: str) -> bool | None:
    s = (resp or "").strip().upper()
    if s in ("1", "ON"):
        return True
    if s in ("0", "OFF"):
        return False
    return None


def _on_off(value: bool) -> str:
    return "ON" if value else "OFF"
