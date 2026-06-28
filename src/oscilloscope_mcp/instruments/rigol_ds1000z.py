"""RIGOL DS1000Z-series oscilloscope driver (DS1054Z / DS1074Z / DS1104Z).

SCPI dialect reference: RIGOL DS1000Z Programming Guide (publicly
available). The DS1000Z line uses raw TCP port 5555 with line-based
SCPI commands and IEEE-488.2 definite-length binary blocks for
``:DISP:DATA?`` and ``:WAV:DATA?``.

Why a single driver covers four models: all DS1000Z variants share the
same instruction set. They differ only in analog bandwidth, which is
captured per-model in the corresponding profile YAML.

Coordinate translation for cursors: the DS1000Z screen is 800 × 480 px
with the time-axis split across 12 horizontal divisions and the voltage
axis across 8 vertical divisions. Cursor SCPI accepts pixel coordinates,
so this module converts seconds / volts into pixels using the current
timebase and per-channel scale.
"""

from __future__ import annotations

import re
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
from oscilloscope_mcp.transport.scpi_lan import ScpiError, ScpiLan


# Display geometry (DS1000Z is fixed at 800 × 480, 12 × 8 divisions).
_PX_PER_TIME_DIV = 50    # 600 / 12
_PX_PER_VOLT_DIV = 50    # 400 / 8 ; 480 px display - 80 px chrome
_TIME_CENTER_PX = 300    # screen middle on time axis
_VOLT_CENTER_PX = 200    # screen middle on voltage axis


class RigolDs1000z(Scope):
    """Driver for any DS1000Z-series scope (DS1054Z, DS1074Z, DS1104Z)."""

    transport: ScpiLan

    # :TRIG:STAT? lags a run-control command (:RUN/:STOP/:SINGle/:TFORce)
    # by ~0.2 s on this firmware — measured. Settle before returning so a
    # subsequent status read reflects the action rather than the stale
    # pre-action value. Tests set this to 0.
    run_status_settle_s: float = 0.3

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
            on = self.transport.query(f":CHAN{ch}:DISP?").strip()
            if on in ("1", "ON"):
                count += 1
        return count

    def timebase_s_per_div(self) -> float:
        return float(self.transport.query(":TIM:MAIN:SCAL?"))

    def channel_scale_v_per_div(self, channel: int) -> float:
        return float(self.transport.query(f":CHAN{channel}:SCAL?"))

    def channel_offset_v(self, channel: int) -> float:
        return float(self.transport.query(f":CHAN{channel}:OFFS?"))

    # ------------------------------------------------------------------
    # Acquisition setup — vertical (channel) + horizontal (timebase)
    # ------------------------------------------------------------------

    def get_channel(self, channel: int) -> ChannelState:
        n = acq_helper.validate_channel_number(self.profile, channel)
        return ChannelState(
            channel=n,
            scale_v_per_div=_parse_float(self.transport.query(f":CHAN{n}:SCAL?")),
            offset_v=_parse_float(self.transport.query(f":CHAN{n}:OFFS?")),
            coupling=self.transport.query(f":CHAN{n}:COUP?").strip() or None,
            display=_parse_bool(self.transport.query(f":CHAN{n}:DISP?")),
            probe=_parse_float(self.transport.query(f":CHAN{n}:PROB?")),
            bw_limit=self.transport.query(f":CHAN{n}:BWL?").strip() or None,
            invert=_parse_bool(self.transport.query(f":CHAN{n}:INV?")),
            units=self.transport.query(f":CHAN{n}:UNIT?").strip() or None,
        )

    def set_channel(self, setup: ChannelSetup) -> ChannelState:
        # Validate the channel number + every requested field up front
        # (raises before any SCPI is sent), then build a single burst.
        n = acq_helper.validate_channel_number(self.profile, setup.channel)
        cmds: list[str] = []
        # Probe FIRST: changing the attenuation ratio re-ranges scale/offset
        # on the instrument, so it must land before scale/offset are set.
        if setup.probe is not None:
            ratio = acq_helper.validate_probe(self.profile, setup.probe)
            cmds.append(f":CHAN{n}:PROB {ratio:g}")
        if setup.coupling is not None:
            cmds.append(f":CHAN{n}:COUP {acq_helper.validate_coupling(self.profile, setup.coupling)}")
        if setup.units is not None:
            cmds.append(f":CHAN{n}:UNIT {acq_helper.validate_units(self.profile, setup.units)}")
        if setup.scale_v_per_div is not None:
            scale = acq_helper.validate_channel_scale(self.profile, setup.scale_v_per_div)
            cmds.append(f":CHAN{n}:SCAL {scale:g}")
        if setup.offset_v is not None:
            offset = acq_helper.validate_channel_offset(self.profile, setup.offset_v)
            cmds.append(f":CHAN{n}:OFFS {offset:g}")
        if setup.bw_limit is not None:
            cmds.append(f":CHAN{n}:BWL {acq_helper.validate_bw_limit(self.profile, setup.bw_limit)}")
        if setup.invert is not None:
            cmds.append(f":CHAN{n}:INV {_on_off(acq_helper.validate_invert(setup.invert))}")
        if setup.display is not None:
            cmds.append(f":CHAN{n}:DISP {_on_off(acq_helper.validate_display(setup.display))}")
        self.transport.write_many(cmds)
        return self.get_channel(n)

    def get_timebase(self) -> TimebaseState:
        return TimebaseState(
            s_per_div=_parse_float(self.transport.query(":TIM:MAIN:SCAL?")),
            offset_s=_parse_float(self.transport.query(":TIM:MAIN:OFFS?")),
            mode=self.transport.query(":TIM:MODE?").strip() or None,
        )

    def set_timebase(self, setup: TimebaseSetup) -> TimebaseState:
        # Validate every requested field up front (raises before any SCPI).
        cmds: list[str] = []
        # Mode FIRST (MAIN/XY/ROLL) so scale/offset apply to the resulting
        # main-window state.
        if setup.mode is not None:
            cmds.append(f":TIM:MODE {acq_helper.validate_timebase_mode(self.profile, setup.mode)}")
        if setup.s_per_div is not None:
            scale = acq_helper.validate_timebase_scale(self.profile, setup.s_per_div)
            cmds.append(f":TIM:MAIN:SCAL {scale:g}")
        if setup.offset_s is not None:
            offset = acq_helper.validate_timebase_offset(self.profile, setup.offset_s)
            cmds.append(f":TIM:MAIN:OFFS {offset:g}")
        self.transport.write_many(cmds)
        return self.get_timebase()

    # ------------------------------------------------------------------
    # Acquisition mode (acquire type / averages / memory depth)
    # ------------------------------------------------------------------

    def get_acquire(self) -> AcquireState:
        acq_type = self.transport.query(":ACQ:TYPE?").strip() or None
        averages = _parse_int(self.transport.query(":ACQ:AVER?"))
        mdepth_raw = self.transport.query(":ACQ:MDEP?").strip()
        if mdepth_raw.upper() == "AUTO":
            memory_depth: str | int | None = "AUTO"
        else:
            memory_depth = _parse_int(mdepth_raw)
        sample_rate = _parse_float(self.transport.query(":ACQ:SRAT?"))
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
            cmds.append(f":ACQ:TYPE {validated_type}")
        if setup.averages is not None:
            validated_avg = acq_helper.validate_averages(self.profile, setup.averages)
            cmds.append(f":ACQ:AVER {validated_avg}")
        if setup.memory_depth is not None:
            active = self.active_channel_count()
            validated_mdepth = acq_helper.validate_memory_depth(
                self.profile, active, setup.memory_depth
            )
            cmds.append(f":ACQ:MDEP {validated_mdepth}")
        self.transport.write_many(cmds)
        return self.get_acquire()

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------

    def get_trigger(self) -> TriggerState:
        mode = trig_helper.keyword_from_query(
            self.profile, self.transport.query(":TRIG:MODE?")
        )
        state = TriggerState(
            mode=mode,
            status=self.transport.query(":TRIG:STAT?").strip() or None,
            sweep=self.transport.query(":TRIG:SWE?").strip() or None,
            coupling=self.transport.query(":TRIG:COUP?").strip() or None,
            holdoff_s=_parse_float(self.transport.query(":TRIG:HOLD?")),
        )
        # Read every readable parameter the current mode declares in its
        # profile schema — fully generic across all 15 trigger types. A
        # param that can't be read (firmware quirk / arg-required query) is
        # skipped rather than failing the whole read.
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
                    self.profile, self.transport.query(":TRIG:MODE?")
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
        # type-specific writes target it), synced with *OPC?. Separate
        # per-command connections would let the instrument drop the param
        # writes that arrive while it is still switching mode.
        cmds: list[str] = []
        if setup.mode is not None:
            cmds.append(f":TRIG:MODE {entry['keyword']}")
        if sweep is not None:
            cmds.append(f":TRIG:SWE {sweep}")
        if coupling is not None:
            cmds.append(f":TRIG:COUP {coupling}")
        if setup.holdoff_s is not None:
            cmds.append(f":TRIG:HOLD {setup.holdoff_s:g}")
        cmds.extend(f"{scpi_node} {value}" for scpi_node, value in param_writes)
        self.transport.write_many(cmds)

        return self.get_trigger()

    def run_control(self, action: str) -> str:
        name, scpi = trig_helper.resolve_run_action(self.profile, action)
        self.transport.write(scpi)
        if self.run_status_settle_s:
            time.sleep(self.run_status_settle_s)  # let :TRIG:STAT? catch up
        return name

    # ------------------------------------------------------------------
    # Automatic measurements
    # ------------------------------------------------------------------

    def measure(
        self,
        items: list[str],
        source: str = "CHAN1",
        source2: str | None = None,
    ) -> dict[str, float | None]:
        # Validate + normalize EVERYTHING up front (raises before any SCPI
        # is sent), mirroring set_trigger's validate-then-send discipline.
        resolved = meas_helper.resolve_items(self.profile, items)
        src = meas_helper.normalize_source(self.profile, source)
        src2 = (
            meas_helper.normalize_source(self.profile, source2)
            if source2 is not None else None
        )
        # A dual-source item without source2 is a caller error — reject it
        # rather than send a malformed :MEAS:ITEM? that the scope rejects.
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
                cmd = f":MEAS:ITEM? {kw},{src},{src2}"
            else:
                cmd = f":MEAS:ITEM? {kw},{src}"
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
    # Measurement statistics (:MEAS:STAT)
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

        # Enable statistics display so the instrument accumulates data.
        self.transport.write(":MEAS:STAT:DISP ON")

        # Map canonical stat-type → MeasureStatResult field name.
        _STAT_FIELD = {
            "CURRent": "current",
            "MAXimum": "maximum",
            "MINimum": "minimum",
            "AVERages": "average",
            "DEViation": "deviation",
            "COUNt": "count",
        }

        results: list[MeasureStatResult] = []
        for name, spec in resolved:
            kw = spec["scpi"]
            result = MeasureStatResult(item=name, source=src)
            for st in all_types:
                cmd = f":MEAS:STAT:ITEM? {st},{kw},{src}"
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

    # Maximum points the DS1000Z returns per :WAV:DATA? call in RAW mode.
    _RAW_CHUNK_SIZE = 250_000

    def read_waveform(self, source: str, mode: str = "NORMal") -> Waveform:
        """Read CH<n> waveform in the specified mode, BYTE format.

        ``mode`` must be ``"NORMal"`` (screen memory, ~1200 pts) or
        ``"RAW"`` (full acquisition memory, up to 24M pts). RAW mode
        requires the scope to be stopped (trigger status STOP or TD);
        raises :class:`RuntimeError` otherwise.
        """
        mode_upper = mode.strip().upper()
        if mode_upper == "RAW":
            return self._read_waveform_raw(source)
        if mode_upper == "NORMAL":
            return self._read_waveform_normal(source)
        raise ValueError(
            f"unsupported waveform mode {mode!r}; expected 'NORMal' or 'RAW'"
        )

    def _read_waveform_normal(self, source: str) -> Waveform:
        """Read CH<n> in NORMal (screen) mode, BYTE format.

        NORMal mode reads the ~1200-point screen memory and does not
        require stopping acquisition, so it is the simplest robust path
        for the quantize → RLE pipeline. The preamble supplies the
        scaling factors; each BYTE sample is an unsigned 0..255 count
        converted to volts via
        ``volts = (raw - yorigin - yreference) * yincrement``.
        """
        src = _normalize_source(source, self._channel_count())
        # Set source + read mode/format together on one connection so the
        # subsequent :WAV:PRE? / :WAV:DATA? see a consistent configuration.
        self.transport.write_many([
            f":WAV:SOURce {src}",
            ":WAV:MODE NORMal",
            ":WAV:FORMat BYTE",
        ])
        pre = _parse_preamble(self.transport.query(":WAV:PREamble?"))
        raw = self.transport.query_binary(":WAV:DATA?")

        yorigin = pre["yorigin"]
        yref = pre["yreference"]
        yinc = pre["yincrement"]
        volts = [(b - yorigin - yref) * yinc for b in raw]

        # time[i] = (i - xreference) * xincrement + xorigin (seconds).
        xinc = pre["xincrement"]
        xorigin = pre["xorigin"]
        xref = pre["xreference"]
        # Adjust so trigger position = t=0 (same as RAW path).
        try:
            trig_pos = int(self.transport.query(":TRIG:POS?"))
        except (ValueError, Exception):
            trig_pos = 0
        if trig_pos < 0:
            trig_pos = 0
        t0_s = (0 - xref) * xinc + xorigin - trig_pos * xinc
        return Waveform(source=src, volts=volts, t0_s=t0_s, dt_s=xinc)

    def _read_waveform_raw(self, source: str) -> Waveform:
        """Read CH<n> in RAW (full memory) mode, BYTE format.

        RAW mode reads the complete ADC acquisition buffer (up to 24M
        points on DS1000Z). The scope MUST be stopped (trigger status
        STOP or TD) before calling this — the acquisition memory is only
        stable when the scope is not running.

        Because the DS1000Z limits each :WAV:DATA? transfer to 250,000
        points, large acquisitions are read in sequential chunks via
        :WAV:STARt / :WAV:STOP windowing.
        """
        # 1. Verify the scope is stopped.
        status = self.transport.query(":TRIG:STAT?").strip().upper()
        if status not in ("STOP", "TD"):
            raise RuntimeError(
                f"scope must be stopped for RAW waveform read (status={status}); "
                "stop acquisition first with scope_trigger(action='STOP') "
                "or use SINGLE sweep"
            )

        src = _normalize_source(source, self._channel_count())

        # 2. Configure source, RAW mode, BYTE format.
        self.transport.write_many([
            f":WAV:SOURce {src}",
            ":WAV:MODE RAW",
            ":WAV:FORMat BYTE",
        ])

        # 3. Read preamble to determine total point count + scaling.
        pre = _parse_preamble(self.transport.query(":WAV:PREamble?"))
        total_points = int(pre["points"])

        # 4. Read in chunks of _RAW_CHUNK_SIZE.
        raw_bytes = bytearray()
        chunk_size = self._RAW_CHUNK_SIZE
        for start in range(1, total_points + 1, chunk_size):
            end = min(start + chunk_size - 1, total_points)
            self.transport.write_many([
                f":WAV:STARt {start}",
                f":WAV:STOP {end}",
            ])
            chunk = self.transport.query_binary(":WAV:DATA?")
            raw_bytes.extend(chunk)

        # 5. Scale to volts using preamble parameters.
        yorigin = pre["yorigin"]
        yref = pre["yreference"]
        yinc = pre["yincrement"]
        volts = [(b - yorigin - yref) * yinc for b in raw_bytes]

        # time[i] = (i - xreference) * xincrement + xorigin (seconds).
        xinc = pre["xincrement"]
        xorigin = pre["xorigin"]
        xref = pre["xreference"]
        # Adjust t0 so trigger position is at t=0. :TRIG:POS? returns
        # the sample index of the trigger in memory; data before that
        # index is pre-trigger.
        try:
            trig_pos = int(self.transport.query(":TRIG:POS?"))
        except (ValueError, Exception):
            trig_pos = 0
        if trig_pos < 0:
            trig_pos = 0  # -2 = not triggered, -1 = outside memory
        t0_s = (0 - xref) * xinc + xorigin - trig_pos * xinc
        return Waveform(source=src, volts=volts, t0_s=t0_s, dt_s=xinc)

    def _channel_count(self) -> int:
        return int(self.profile.get("capability", {}).get("channels", 4))

    # ------------------------------------------------------------------
    # Screenshot + annotation
    # ------------------------------------------------------------------

    def screenshot(self, plan: ScreenshotPlan) -> ScreenshotResult:
        # 1. Channel labels.
        labels_applied: dict[int, str] = {}
        if plan.channel_labels:
            for ch, label in plan.channel_labels.items():
                safe = _sanitize_ascii_label(label)
                self.transport.write(f':CHAN{ch}:LAB "{safe}"')
                labels_applied[ch] = safe
            if plan.display_labels or labels_applied:
                self.transport.write(":DISP:LABS ON")

        # 2. Cursors.
        cursors_applied: list[CursorPair] = []
        if plan.cursor_pairs:
            self.transport.write(":CURS:MODE MAN")
            for pair in plan.cursor_pairs:
                applied = self._apply_cursor_pair(pair)
                cursors_applied.append(applied)

        # 3. Screenshot.
        fmt = (plan.image_format or "PNG").upper()
        if fmt not in {"PNG", "BMP"}:
            raise ValueError(
                f"unsupported screenshot format {fmt!r}; profile lists "
                f"{self.profile.get('capability', {}).get('screenshot_formats')}"
            )
        # :DISP:DATA? ON,0,PNG — color, no invert, PNG (or BMP)
        payload = self.transport.query_binary(f":DISP:DATA? ON,0,{fmt}")
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
        """Translate seconds/volts into pixel SCPI and apply."""
        self.transport.write(f":CURS:MAN:SOUR CHAN{pair.source_channel}")
        if pair.ax_t_s is not None or pair.bx_t_s is not None:
            t_per_div = self.timebase_s_per_div()
            if pair.ax_t_s is not None:
                px = _time_to_px(pair.ax_t_s, t_per_div)
                self.transport.write(f":CURS:MAN:AX {px}")
            if pair.bx_t_s is not None:
                px = _time_to_px(pair.bx_t_s, t_per_div)
                self.transport.write(f":CURS:MAN:BX {px}")
        if pair.ay_v is not None or pair.by_v is not None:
            v_per_div = self.channel_scale_v_per_div(pair.source_channel)
            offset = self.channel_offset_v(pair.source_channel)
            if pair.ay_v is not None:
                py = _volt_to_px(pair.ay_v, v_per_div, offset)
                self.transport.write(f":CURS:MAN:AY {py}")
            if pair.by_v is not None:
                py = _volt_to_px(pair.by_v, v_per_div, offset)
                self.transport.write(f":CURS:MAN:BY {py}")
        return pair


# ---------------------------------------------------------------------------
# Coordinate translation
# ---------------------------------------------------------------------------


def _time_to_px(t_s: float, t_per_div: float) -> int:
    """Translate a time value (seconds from trigger) into a screen pixel
    on the horizontal axis. Trigger lives at the screen center.
    """
    if t_per_div <= 0:
        raise ValueError(f"timebase must be positive, got {t_per_div}")
    return int(round(_TIME_CENTER_PX + (t_s / t_per_div) * _PX_PER_TIME_DIV))


def _volt_to_px(v: float, v_per_div: float, offset_v: float) -> int:
    """Translate a voltage value into a screen pixel on the vertical axis.
    Positive volts go *up* the screen, which corresponds to *lower*
    pixel-y values (origin top-left).
    """
    if v_per_div <= 0:
        raise ValueError(f"voltage scale must be positive, got {v_per_div}")
    return int(round(_VOLT_CENTER_PX - ((v - offset_v) / v_per_div) * _PX_PER_VOLT_DIV))


# Order of the 10 comma-separated :WAV:PREamble? fields (DS1000Z).
_PREAMBLE_FIELDS = (
    "format", "type", "points", "count",
    "xincrement", "xorigin", "xreference",
    "yincrement", "yorigin", "yreference",
)


def _parse_preamble(resp: str) -> dict[str, float]:
    """Parse the 10-field ``:WAV:PREamble?`` CSV into a typed dict.

    ``points`` / ``count`` / ``*reference`` are integers; the increments
    and origins are floats. Raises :class:`ScpiError` on a malformed
    response so the failure surfaces as a clean transport error.
    """
    parts = [p.strip() for p in resp.split(",")]
    if len(parts) < len(_PREAMBLE_FIELDS):
        raise ScpiError(
            f"unexpected :WAV:PREamble? response (need "
            f"{len(_PREAMBLE_FIELDS)} fields, got {len(parts)}): {resp!r}"
        )
    try:
        return {name: float(parts[i]) for i, name in enumerate(_PREAMBLE_FIELDS)}
    except ValueError as e:
        raise ScpiError(f"malformed :WAV:PREamble? field: {resp!r}") from e


def _normalize_source(source: str, channel_count: int) -> str:
    """Normalize a source like ``"1"`` / ``"ch2"`` / ``"CHANnel3"`` to the
    canonical ``CHAN<n>`` form, bounded by the model's channel count.
    """
    s = str(source).strip().upper()
    m = re.fullmatch(r"(?:CH(?:AN(?:NEL)?)?)?([0-9]+)", s)
    if not m:
        raise ValueError(
            f"waveform source {source!r} invalid; expected CHAN1..CHAN{channel_count}"
        )
    n = int(m.group(1))
    if not 1 <= n <= channel_count:
        raise ValueError(
            f"waveform source channel {n} out of range (1..{channel_count})"
        )
    return f"CHAN{n}"


def _parse_float(resp: str) -> float | None:
    """Parse a numeric SCPI response, returning None on anything the
    instrument couldn't express as a number (empty / 'NONE' / junk).
    """
    try:
        return float(resp.strip())
    except (ValueError, AttributeError):
        return None


def _parse_int(resp: str) -> int | None:
    """Parse an integer SCPI response, returning None on anything the
    instrument couldn't express as a number (empty / 'NONE' / junk).
    Handles scientific notation (e.g. ``"1.200000e+04"`` → 12000).
    """
    try:
        return int(float(resp.strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_bool(resp: str) -> bool | None:
    """Parse a boolean SCPI response (``1``/``0``/``ON``/``OFF``) into a
    Python bool, returning None on anything unrecognized.
    """
    s = (resp or "").strip().upper()
    if s in ("1", "ON"):
        return True
    if s in ("0", "OFF"):
        return False
    return None


def _on_off(value: bool) -> str:
    """Render a bool as the RIGOL ``ON``/``OFF`` keyword."""
    return "ON" if value else "OFF"


def _sanitize_ascii_label(label: str) -> str:
    """DS1000Z channel labels accept ASCII only (and length-limited).
    Strip non-ASCII and truncate to 10 characters to match the on-screen
    label box.
    """
    ascii_only = "".join(c for c in label if 32 <= ord(c) < 127 and c != '"')
    return ascii_only[:10]
