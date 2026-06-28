"""FastMCP server exposing the bench-scope tools.

The server is intentionally minimal: each tool resolves the scope via
:func:`oscilloscope_mcp.instruments.open_scope` (env-driven or explicit kwargs),
runs the operation, and returns a structured dict with a mandatory
``caveats`` field.

Transport: **stdio** (default for MCP servers). The repo's trust model
is local-only (see ``CLAUDE.md §利用想定``), so no network endpoint is
exposed.

Run with::

    python -m oscilloscope_mcp
    # or
    oscilloscope-mcp

Register with Claude Code::

    claude mcp add bench-scope -- python -m oscilloscope_mcp
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from oscilloscope_mcp.helpers import bus as bus_helper
from oscilloscope_mcp.helpers import edges as edges_helper
from oscilloscope_mcp.helpers import measure as meas_helper
from oscilloscope_mcp.helpers import quantize as quantize_helper
from oscilloscope_mcp.helpers import reference_diff as refdiff_helper
from oscilloscope_mcp.helpers import rle as rle_helper
from oscilloscope_mcp.helpers import trigger as trig_helper
from oscilloscope_mcp.helpers.caveat_calc import (
    caveat_for_bw_limit,
    caveat_for_cursor_readout,
    caveat_for_raw_scpi,
    caveat_for_timebase,
    caveat_for_unmeasurable_items,
    caveat_for_waveform_quantize,
    compute_caveats,
)
from oscilloscope_mcp.instruments import (
    ScopeDispatchError,
    open_scope,
)
from oscilloscope_mcp.instruments._base import (
    AcquireSetup,
    ChannelSetup,
    CursorPair,
    MeasureStatResult,
    ScreenshotPlan,
    TimebaseSetup,
    TriggerSetup,
)
from oscilloscope_mcp.transport.scpi_lan import ScpiError


# Cap on the number of runs/edges returned, so a pathologically noisy
# capture can't blow past the ~10 KB response budget (ROADMAP P1.5 DoD).
# A clean square wave is a handful of runs; this only bites on noise.
_MAX_RUNS = 400


SERVER_NAME = "oscilloscope-mcp"


def build_server() -> FastMCP:
    """Construct the FastMCP server with both tools registered.

    Exposed as a function (rather than module-level state) so unit tests
    can build a fresh server per test if needed, and so the entry-point
    in :mod:`__main__` has a single call site to keep in sync.
    """
    mcp = FastMCP(SERVER_NAME)

    @mcp.tool(
        name="scope_query",
        description=(
            "Raw SCPI command passthrough to a bench oscilloscope. Use a "
            "'?'-terminated command (e.g. ':TRIG:STAT?') for queries; "
            "anything else is a write. Returns the response plus a "
            "caveats[] warning since instrument-limit checks (sample rate, "
            "bandwidth, memory depth) are not auto-applied on this path."
        ),
    )
    def scope_query(
        scpi: str,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        if not scpi or not scpi.strip():
            raise ValueError("'scpi' is required and must be non-empty")
        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            response = scope.query_raw(scpi)
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e
        return {
            "response": response,
            "is_query": "?" in scpi,
            "caveats": caveat_for_raw_scpi(),
        }

    @mcp.tool(
        name="scope_screenshot",
        description=(
            "Save a PNG of the current scope screen for user review. "
            "Optionally place manual cursors (Δt / ΔV markers on the "
            "waveform) and channel labels (e.g. DIR/STP/NXT/CLK) so the "
            "image is self-explanatory. Cursor read-outs are bounded by "
            "display pixel resolution — for precise numbers, use "
            "scope_query with a :MEAS:ITEM? command instead."
        ),
    )
    def scope_screenshot(
        output_path: str,
        cursors: list[dict[str, Any]] | None = None,
        channel_labels: dict[str, str] | None = None,
        display_labels: bool = False,
        image_format: str = "PNG",
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        out_path = Path(output_path).expanduser()
        if out_path.exists() and out_path.is_dir():
            raise ValueError(
                f"'output_path' must be a file path, not a directory: {out_path}"
            )

        plan = ScreenshotPlan(image_format=image_format.upper())
        plan.display_labels = bool(display_labels)
        if cursors:
            for spec in cursors:
                plan.cursor_pairs.append(_parse_cursor(spec))
        if channel_labels:
            for k, v in channel_labels.items():
                plan.channel_labels[int(k)] = str(v)

        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            result = scope.screenshot(plan)
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(result.image_bytes)

        caveats: list[str] = []
        if result.cursors_set:
            caveats.extend(caveat_for_cursor_readout())

        return {
            "screenshot_path": str(out_path),
            "bytes_written": len(result.image_bytes),
            "image_format": result.image_format,
            "cursors_set": [_cursor_to_dict(c) for c in result.cursors_set],
            "channel_labels_applied": {
                str(k): v for k, v in result.channel_labels_applied.items()
            },
            "caveats": caveats,
        }

    @mcp.tool(
        name="scope_waveform",
        description=(
            "Capture one channel's waveform and return it as a compact "
            "threshold-quantized event stream — NOT raw ADC samples. The "
            "raw volts are run through a Schmitt-trigger comparator "
            "(threshold_v ± hysteresis_v/2) into a 0/1 digital stream, then "
            "run-length-encoded into 'runs' [(t_us, level, dur_us)] and an "
            "'edges' list [(t_us, channel, RISE|FALL)]. Hysteresis is "
            "mandatory: a single threshold on a noisy edge fragments into "
            "useless micro-pulses. If hysteresis_v exceeds 20% of the "
            "signal peak-to-peak, a detection-miss caveat is emitted; if the "
            "stream is truncated to fit the size budget, a truncation caveat "
            "is emitted. Times are microseconds, trigger at t=0. "
            "Set mode='RAW' to read the FULL acquisition memory (up to 24M "
            "points) instead of the ~1200-point screen decimation — the scope "
            "MUST be stopped first (use scope_trigger(action='STOP') or SINGLE "
            "sweep). RAW mode is much slower but captures every ADC sample."
        ),
    )
    def scope_waveform(
        source: str = "CHAN1",
        mode: str = "NORMal",
        threshold_v: float = 1.5,
        hysteresis_v: float = 0.1,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        if hysteresis_v < 0:
            raise ValueError("'hysteresis_v' must be >= 0")
        mode_upper = mode.strip().upper()
        if mode_upper not in ("NORMAL", "RAW"):
            raise ValueError(
                f"'mode' must be 'NORMal' or 'RAW', got {mode!r}"
            )
        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            wf = scope.read_waveform(source, mode=mode)
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        levels = quantize_helper.quantize(wf.volts, threshold_v, hysteresis_v)
        runs = rle_helper.runs_from_levels(
            levels, t0_us=wf.t0_s * 1e6, dt_us=wf.dt_s * 1e6
        )
        truncated = len(runs) > _MAX_RUNS
        if truncated:
            runs = runs[:_MAX_RUNS]
        edges = edges_helper.edges_from_runs(runs, wf.source)

        caveats = caveat_for_waveform_quantize(
            hysteresis_v=hysteresis_v,
            peak_to_peak_v=quantize_helper.peak_to_peak(wf.volts),
            truncated=truncated,
        )
        if mode_upper == "RAW":
            caveats.append(
                f"RAW mode: {wf.n_samples} points read from full acquisition "
                "memory — this is the complete un-decimated ADC record."
            )
        return {
            "source": wf.source,
            "mode": mode_upper,
            "runs": [list(r) for r in runs],
            "edges": [list(e) for e in edges],
            "n_samples": wf.n_samples,
            "caveats": caveats,
        }

    @mcp.tool(
        name="scope_trigger",
        description=(
            "Read or configure the oscilloscope trigger — supports every "
            "trigger type the instrument has. Call with NO arguments to "
            "READ the current state (returns the mode, global settings, the "
            "mode-specific 'params', and 'accepted_params' describing what "
            "you may set for that mode). To SET, pass 'mode' and/or the "
            "global 'sweep'/'coupling'/'holdoff_s', and put all type-"
            "specific parameters in the 'params' object, e.g. "
            "{'source':'CHAN1','slope':'NEG','nth_edge':2,'idle_s':2e-3} for "
            "an Nth-edge trigger. 'mode' is validated against the model "
            "profile's full type list (EDGE/PULSe/SLOPe/VIDeo/PATTern/"
            "DURation plus option-licensed TIMeout/RUNT/WIND/DELay/SHOLd/"
            "NEDG/RS232/IIC/SPI); each params key/value is validated against "
            "that mode's schema (enum membership, numeric range, channel "
            "source). An unsupported type or parameter is rejected with the "
            "valid list. Option-licensed types and any selection that fails "
            "to apply produce a caveats[] warning. Pass 'action' to control "
            "acquisition after configuring: RUN / STOP / SINGLE (arm one "
            "shot) / FORCE (force a trigger now; NORMAL or SINGLE sweep "
            "only). Read 'status' (TD/WAIT/RUN/AUTO/STOP) to see the result."
        ),
    )
    def scope_trigger(
        mode: str | None = None,
        params: dict[str, Any] | None = None,
        sweep: str | None = None,
        coupling: str | None = None,
        holdoff_s: float | None = None,
        action: str | None = None,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        setup = TriggerSetup(
            mode=mode,
            sweep=sweep,
            coupling=coupling,
            holdoff_s=holdoff_s,
            params=dict(params or {}),
        )
        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            # Validate the action up front (no SCPI) so a bad action is
            # rejected before any config is applied.
            if action is not None:
                trig_helper.resolve_run_action(scope.profile, action)
            # Validation (TriggerValidationError → ValueError) happens
            # inside set_trigger before any SCPI write.
            state = scope.set_trigger(setup) if setup.has_setup() else None
            action_applied = (
                scope.run_control(action) if action is not None else None
            )
            if state is None or action is not None:
                # Pure read, or re-read so status reflects the action.
                state = scope.get_trigger()
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        caveats: list[str] = []
        if mode is not None:
            entry = trig_helper.normalize_trigger_keyword(scope.profile, mode)
            caveats.extend(trig_helper.caveats_for_trigger_type(entry))
            # Runtime confirmation: did the requested mode actually stick?
            if state.mode.upper() != str(entry["keyword"]).upper():
                caveats.append(
                    f"requested trigger mode {entry['keyword']!r} but "
                    f":TRIG:MODE? reports {state.mode!r} — the selection did "
                    "not apply (most likely an unlicensed option)."
                )

        # Describe what's settable for the resulting mode (discovery aid).
        cur_entry = trig_helper.normalize_trigger_keyword(scope.profile, state.mode)
        return {
            "mode": state.mode,
            "status": state.status,
            "sweep": state.sweep,
            "coupling": state.coupling,
            "holdoff_s": state.holdoff_s,
            "params": state.params,
            "accepted_params": trig_helper.describe_mode_params(cur_entry),
            "was_set": setup.has_setup(),
            "action_applied": action_applied,
            "caveats": caveats,
        }

    @mcp.tool(
        name="scope_measure",
        description=(
            "Read scope-side automatic measurements (:MEAS:ITEM?) for one "
            "or more items on a source channel — far more precise than "
            "reading cursor coordinates off a screenshot. Pass 'items' as a "
            "list of canonical names; each is validated against the model "
            "profile and an unknown item is rejected with the valid list. "
            "Single-source items: VMAX VMIN VPP VTOP VBASE VAMP VAVG VRMS "
            "OVERSHOOT PRESHOOT PERIOD FREQUENCY RTIME FTIME PWIDTH NWIDTH "
            "PDUTY NDUTY. Two-source items (require 'source2'): RDELAY "
            "FDELAY RPHASE FPHASE. 'source' / 'source2' accept a bare "
            "channel number or CHAN<n> (bounded by the model's channel "
            "count). Returns {'source', 'measurements': {item: value or "
            "null}, 'units', 'caveats'}: a value the instrument reports as "
            "un-measurable (~9.9e37 — e.g. FREQUENCY on a flat or "
            "un-triggered trace) comes back as null and is named in "
            "caveats[]. Observation-limit caveats (analog BW, multi-channel "
            "sample-rate downgrade, memory depth) are attached when the "
            "current setup is outside a profile-declared limit."
        ),
    )
    def scope_measure(
        items: list[str],
        source: str = "CHAN1",
        source2: str | None = None,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            resolved = meas_helper.resolve_items(scope.profile, items)
            norm_source = meas_helper.normalize_source(scope.profile, source)
            norm_source2 = (
                meas_helper.normalize_source(scope.profile, source2)
                if source2 is not None else None
            )
            measurements = scope.measure(items, source, source2)
            active = scope.active_channel_count()
            timebase = scope.timebase_s_per_div()
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        caveats = compute_caveats(
            scope.profile,
            active_channel_count=active,
            timebase_s_per_div=timebase,
        )
        unmeasurable = [name for name, val in measurements.items() if val is None]
        caveats.extend(caveat_for_unmeasurable_items(unmeasurable))

        return {
            "source": norm_source,
            "source2": norm_source2,
            "measurements": measurements,
            "units": meas_helper.units_for(resolved),
            "caveats": caveats,
        }

    @mcp.tool(
        name="scope_measure_stat",
        description=(
            "Read measurement statistics (:MEASure:STATistic:ITEM) for one "
            "or more items on a source channel — provides accumulated "
            "statistical data (current, max, min, average, deviation, count) "
            "rather than a single instantaneous value. Pass 'items' as a "
            "list of canonical measurement names (same as scope_measure). "
            "'stat_types' defaults to all six: CURRent MAXimum MINimum "
            "AVERages DEViation COUNt; pass a subset to limit the query. "
            "'mode' optionally sets the statistics mode (DIFFerence or "
            "EXTRemum) before querying. 'reset=True' resets accumulated "
            "statistics before querying. Returns {'source', 'statistics': "
            "[{item, current, max, min, avg, dev, count}, ...], 'caveats'}."
        ),
    )
    def scope_measure_stat(
        items: list[str],
        source: str = "CHAN1",
        stat_types: list[str] | None = None,
        mode: str | None = None,
        reset: bool = False,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            # Validate items + source (same as scope_measure).
            meas_helper.resolve_items(scope.profile, items)
            norm_source = meas_helper.normalize_source(scope.profile, source)

            # Validate stat_types if provided.
            if stat_types is not None:
                for st in stat_types:
                    meas_helper.validate_stat_type(scope.profile, st)

            # Validate and optionally set mode.
            if mode is not None:
                validated_mode = meas_helper.validate_stat_mode(scope.profile, mode)
                scope.transport.write(f":MEAS:STAT:MODE {validated_mode}")

            # Optionally reset accumulated statistics.
            if reset:
                scope.transport.write(":MEAS:STAT:RES")

            stat_results = scope.measure_statistics(items, source, stat_types)
            active = scope.active_channel_count()
            timebase = scope.timebase_s_per_div()
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        caveats = compute_caveats(
            scope.profile,
            active_channel_count=active,
            timebase_s_per_div=timebase,
        )

        # Build the response statistics list.
        statistics: list[dict[str, Any]] = []
        for r in stat_results:
            entry: dict[str, Any] = {"item": r.item}
            if r.current is not None:
                entry["current"] = r.current
            if r.maximum is not None:
                entry["max"] = r.maximum
            if r.minimum is not None:
                entry["min"] = r.minimum
            if r.average is not None:
                entry["avg"] = r.average
            if r.deviation is not None:
                entry["dev"] = r.deviation
            if r.count is not None:
                entry["count"] = r.count
            statistics.append(entry)

        return {
            "source": norm_source,
            "statistics": statistics,
            "caveats": caveats,
        }

    @mcp.tool(
        name="scope_channel",
        description=(
            "Read or configure an oscilloscope analog channel's VERTICAL "
            "setup. 'channel' (1-based) is required and validated against "
            "the model's channel count. Call with ONLY 'channel' to READ "
            "the current state. To SET, pass any subset of: "
            "'scale_v_per_div', 'offset_v', 'coupling' (AC/DC/GND), "
            "'display' (on/off), 'probe' (attenuation ratio), "
            "'bw_limit' (20M/OFF), 'invert' (on/off), 'units'. "
            "Each value is validated against the model profile; an invalid "
            "value is rejected with the valid list before any SCPI is sent. "
            "Engaging a 20 MHz bandwidth limit emits a caveats[] warning."
        ),
    )
    def scope_channel(
        channel: int,
        scale_v_per_div: float | None = None,
        offset_v: float | None = None,
        coupling: str | None = None,
        display: bool | None = None,
        probe: float | None = None,
        bw_limit: str | None = None,
        invert: bool | None = None,
        units: str | None = None,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        setup = ChannelSetup(
            channel=channel,
            scale_v_per_div=scale_v_per_div,
            offset_v=offset_v,
            coupling=coupling,
            display=display,
            probe=probe,
            bw_limit=bw_limit,
            invert=invert,
            units=units,
        )
        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            state = (
                scope.set_channel(setup)
                if setup.has_setup()
                else scope.get_channel(channel)
            )
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        caveats: list[str] = []
        if state.bw_limit is not None:
            caveats.extend(caveat_for_bw_limit(scope.profile, state.bw_limit))

        return {
            "channel": state.channel,
            "scale_v_per_div": state.scale_v_per_div,
            "offset_v": state.offset_v,
            "coupling": state.coupling,
            "display": state.display,
            "probe": state.probe,
            "bw_limit": state.bw_limit,
            "invert": state.invert,
            "units": state.units,
            "was_set": setup.has_setup(),
            "caveats": caveats,
        }

    @mcp.tool(
        name="scope_timebase",
        description=(
            "Read or configure the oscilloscope HORIZONTAL (timebase) "
            "setup. Call with NO arguments to READ the current state "
            "(s_per_div, offset_s, mode). To SET, pass any subset of: "
            "'s_per_div', 'offset_s', 'mode' (MAIN/XY/ROLL). "
            "Values are validated against the model profile; an invalid "
            "value is rejected with the valid list before any SCPI is sent. "
            "A long timebase that exceeds per-channel memory emits a "
            "caveats[] warning."
        ),
    )
    def scope_timebase(
        s_per_div: float | None = None,
        offset_s: float | None = None,
        mode: str | None = None,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        setup = TimebaseSetup(s_per_div=s_per_div, offset_s=offset_s, mode=mode)
        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            state = (
                scope.set_timebase(setup)
                if setup.has_setup()
                else scope.get_timebase()
            )
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        caveats: list[str] = []
        if state.s_per_div is not None:
            caveats.extend(
                caveat_for_timebase(
                    scope.profile,
                    active_channel_count=scope.active_channel_count(),
                    timebase_s_per_div=state.s_per_div,
                )
            )

        return {
            "s_per_div": state.s_per_div,
            "offset_s": state.offset_s,
            "mode": state.mode,
            "was_set": setup.has_setup(),
            "caveats": caveats,
        }

    @mcp.tool(
        name="scope_acquire",
        description=(
            "Read or configure the oscilloscope acquisition mode — type "
            "(NORMal/AVERages/PEAK/HRESolution), averaging count, and "
            "memory depth. Call with NO arguments to READ the current state "
            "(type, averages, memory_depth, sample_rate). To SET, pass any "
            "subset of: 'type', 'averages' (2..1024 powers of 2), "
            "'memory_depth' ('AUTO' or a numeric value depending on active "
            "channel count). Values are validated against the model profile; "
            "an invalid value is rejected with the valid list before any "
            "SCPI is sent. Selecting AVERages mode emits a caveats[] warning "
            "that the update rate is reduced."
        ),
    )
    def scope_acquire(
        type: str | None = None,
        averages: int | None = None,
        memory_depth: str | int | None = None,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        setup = AcquireSetup(
            type=type, averages=averages, memory_depth=memory_depth
        )
        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            state = (
                scope.set_acquire(setup)
                if setup.has_setup()
                else scope.get_acquire()
            )
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        caveats: list[str] = []
        # Warn when averaging is active (either just set or read back).
        if state.type is not None and state.type.upper() in ("AVER", "AVERAGES"):
            caveats.append(
                "averaging mode slows update rate — each displayed "
                "waveform is the mean of multiple acquisitions; real-time "
                "events may be missed."
            )

        return {
            "type": state.type,
            "averages": state.averages,
            "memory_depth": state.memory_depth,
            "sample_rate": state.sample_rate,
            "was_set": setup.has_setup(),
            "caveats": caveats,
        }

    # ------------------------------------------------------------------
    # scope_capture — declarative single-shot capture (P2 culmination)
    # ------------------------------------------------------------------

    @mcp.tool(
        name="scope_capture",
        description=(
            "Declarative single-shot capture — configure, arm, wait, read in "
            "one call. Composes trigger/channel/timebase/acquire setup, arms "
            "a :SINGle acquisition, polls until triggered (or timeout), then "
            "reads per-channel waveforms (quantized to runs/edges), scope-side "
            "measurements, and an optional screenshot. Returns structured "
            "results with caveats[]. Use sweep='AUTO' or 'NORMAL' to skip the "
            "arm+poll step and read the current screen memory immediately. "
            "Set waveform_mode='RAW' to read the full acquisition memory "
            "(up to 24M points) instead of screen decimation — the scope must "
            "be stopped (automatic after SINGLE trigger)."
        ),
    )
    def scope_capture(
        # --- trigger ---
        trigger_mode: str | None = None,
        trigger_params: dict[str, Any] | None = None,
        sweep: str = "SINGLE",
        # --- channels ---
        channels: list[str] | None = None,
        channel_settings: dict[str, Any] | None = None,
        # --- waveform quantization ---
        threshold_v: float = 1.5,
        hysteresis_v: float = 0.1,
        # --- waveform mode ---
        waveform_mode: str = "NORMal",
        # --- measurements ---
        measurements: list[str] | None = None,
        # --- acquire ---
        acquire_type: str | None = None,
        memory_depth: str | int | None = None,
        # --- timebase ---
        timebase_s_per_div: float | None = None,
        timebase_offset_s: float | None = None,
        # --- output ---
        screenshot_path: str | None = None,
        timeout_s: float = 10.0,
        # --- connection ---
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if hysteresis_v < 0:
            raise ValueError("'hysteresis_v' must be >= 0")
        waveform_mode_upper = waveform_mode.strip().upper()
        if waveform_mode_upper not in ("NORMAL", "RAW"):
            raise ValueError(
                f"'waveform_mode' must be 'NORMal' or 'RAW', got {waveform_mode!r}"
            )

        try:
            scope = open_scope(host=host, port=port, model=model, timeout_s=timeout_s)
        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e

        caveats: list[str] = []

        try:
            # 1. Apply settings -----------------------------------------------

            # Timebase
            if timebase_s_per_div is not None or timebase_offset_s is not None:
                tb_setup = TimebaseSetup(
                    s_per_div=timebase_s_per_div,
                    offset_s=timebase_offset_s,
                )
                scope.set_timebase(tb_setup)

            # Acquire mode
            if acquire_type is not None or memory_depth is not None:
                acq_setup = AcquireSetup(
                    type=acquire_type,
                    memory_depth=memory_depth,
                )
                scope.set_acquire(acq_setup)

            # Channel settings
            if channel_settings:
                for ch_key, settings in channel_settings.items():
                    ch_setup = ChannelSetup(
                        channel=int(ch_key) if ch_key.isdigit() else int(
                            ch_key.upper().replace("CHAN", "").replace("NEL", "")
                        ),
                        scale_v_per_div=settings.get("scale_v_per_div"),
                        offset_v=settings.get("offset_v"),
                        coupling=settings.get("coupling"),
                        display=settings.get("display"),
                        probe=settings.get("probe"),
                        bw_limit=settings.get("bw_limit"),
                        invert=settings.get("invert"),
                        units=settings.get("units"),
                    )
                    scope.set_channel(ch_setup)

            # Trigger
            if trigger_mode is not None or trigger_params is not None:
                trig_setup = TriggerSetup(
                    mode=trigger_mode,
                    params=dict(trigger_params or {}),
                )
                scope.set_trigger(trig_setup)

            # 2. Arm + poll (SINGLE mode only) --------------------------------

            sweep_upper = sweep.strip().upper()
            triggered = True

            if sweep_upper == "SINGLE":
                scope.run_control("SINGLE")

                # Poll :TRIG:STAT? every 0.3s until TD/STOP or timeout
                poll_interval = 0.3
                elapsed = 0.0
                triggered = False
                while elapsed < timeout_s:
                    state = scope.get_trigger()
                    status = (state.status or "").upper()
                    if status in ("TD", "STOP"):
                        triggered = True
                        break
                    time.sleep(poll_interval)
                    elapsed += poll_interval

                if not triggered:
                    caveats.append(
                        f"trigger condition not met within {timeout_s}s timeout; "
                        "reading best-effort screen memory (may be stale or incomplete)."
                    )
            # For AUTO/NORMAL/other sweeps, just read immediately (no arm+poll)

            # 3. Determine channels to read -----------------------------------

            if channels is not None:
                ch_list = [c.strip().upper() for c in channels]
            else:
                # Read all displayed channels
                ch_count = int(
                    scope.profile.get("capability", {}).get("channels", 4)
                )
                ch_list = []
                for ch_num in range(1, ch_count + 1):
                    ch_state = scope.get_channel(ch_num)
                    if ch_state.display:
                        ch_list.append(f"CHAN{ch_num}")
                if not ch_list:
                    ch_list = ["CHAN1"]  # fallback

            # 4. Read waveforms -----------------------------------------------

            waveform_result: dict[str, dict[str, Any]] = {}
            all_channel_runs: list[list] = []  # for bus_runs

            for ch in ch_list:
                wf = scope.read_waveform(ch, mode=waveform_mode)
                levels = quantize_helper.quantize(wf.volts, threshold_v, hysteresis_v)
                runs = rle_helper.runs_from_levels(
                    levels, t0_us=wf.t0_s * 1e6, dt_us=wf.dt_s * 1e6
                )
                truncated = len(runs) > _MAX_RUNS
                if truncated:
                    runs = runs[:_MAX_RUNS]
                edges = edges_helper.edges_from_runs(runs, wf.source)

                waveform_result[wf.source] = {
                    "runs": [list(r) for r in runs],
                    "edges": [list(e) for e in edges],
                    "n_samples": wf.n_samples,
                }
                all_channel_runs.append(runs)

                # Per-channel quantize caveats
                pp = quantize_helper.peak_to_peak(wf.volts)
                ch_caveats = caveat_for_waveform_quantize(
                    hysteresis_v=hysteresis_v,
                    peak_to_peak_v=pp,
                    truncated=truncated,
                )
                caveats.extend(ch_caveats)

            # 5. Bus runs (multi-channel) -------------------------------------

            bus_runs_result: list[list] | None = None
            if len(ch_list) > 1:
                bus_runs = bus_helper.bus_runs_from_channels(all_channel_runs)
                bus_runs_result = [list(br) for br in bus_runs]

            # 6. Measurements -------------------------------------------------

            measurements_result: dict[str, dict[str, float | None]] | None = None
            if measurements:
                measurements_result = {}
                for ch in ch_list:
                    meas = scope.measure(measurements, source=ch)
                    measurements_result[ch] = meas
                    # Check for unmeasurable items
                    unmeasurable = [n for n, v in meas.items() if v is None]
                    if unmeasurable:
                        caveats.extend(caveat_for_unmeasurable_items(unmeasurable))

            # 7. Screenshot ---------------------------------------------------

            screenshot_out: str | None = None
            if screenshot_path is not None:
                out_path = Path(screenshot_path).expanduser()
                plan = ScreenshotPlan(image_format="PNG")
                # Add channel labels from the channel list
                for i, ch in enumerate(ch_list):
                    ch_num = int(ch.replace("CHAN", ""))
                    plan.channel_labels[ch_num] = ch
                plan.display_labels = True
                result_ss = scope.screenshot(plan)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(result_ss.image_bytes)
                screenshot_out = str(out_path)

            # 8. Observation-limit caveats ------------------------------------

            active = scope.active_channel_count()
            timebase = scope.timebase_s_per_div()
            obs_caveats = compute_caveats(
                scope.profile,
                active_channel_count=active,
                timebase_s_per_div=timebase,
            )
            caveats.extend(obs_caveats)

        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        return {
            "triggered": triggered,
            "channels": ch_list,
            "waveform": waveform_result,
            "bus_runs": bus_runs_result,
            "measurements": measurements_result,
            "screenshot_path": screenshot_out,
            "caveats": caveats,
        }

    # ------------------------------------------------------------------
    # scope_compare — sim vs HW reference diff (P4)
    # ------------------------------------------------------------------

    @mcp.tool(
        name="scope_compare",
        description=(
            "Compare a reference (simulator) signal against hardware (or "
            "provided hw data). Takes a golden edge/run list from your RTL "
            "sim and a captured edge/run list from the scope, then returns "
            "a structured diff: matched, shifted (with per-edge delta), "
            "missing (in ref not hw), added (in hw not ref), and the "
            "first_divergence_us pinpointing the earliest mismatch. "
            "Two modes: (1) LIVE — captures from the scope if hw_runs / "
            "hw_edges not provided; (2) OFFLINE — pass hw_runs or hw_edges "
            "directly, no scope connection needed. The reference can be "
            "given as runs (list of [t_us, level, dur_us]) or edges (list "
            "of [t_us, kind]). tolerance_us (default 0.05 = 50 ns) sets "
            "how close two edges must be to count as a match."
        ),
    )
    def scope_compare(
        reference: list[list] | None = None,
        reference_edges: list[list] | None = None,
        tolerance_us: float = 0.05,
        source: str = "CHAN1",
        threshold_v: float = 1.5,
        hysteresis_v: float = 0.1,
        hw_runs: list[list] | None = None,
        hw_edges: list[list] | None = None,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        if reference is None and reference_edges is None:
            raise ValueError(
                "either 'reference' (runs) or 'reference_edges' must be "
                "provided as the golden signal from the simulator"
            )
        if tolerance_us < 0:
            raise ValueError(f"'tolerance_us' must be >= 0, got {tolerance_us}")

        # --- Build ref_edges from whichever input was given ---
        if reference_edges is not None:
            ref_edges = [
                edges_helper.Edge(t_us=float(e[0]), channel="REF", kind=str(e[1]))
                for e in reference_edges
            ]
        else:
            # reference is runs: [[t_us, level, dur_us], ...]
            ref_runs = [
                rle_helper.Run(t_us=float(r[0]), level=int(r[1]), dur_us=float(r[2]))
                for r in reference  # type: ignore[union-attr]
            ]
            ref_edges = refdiff_helper.runs_to_edges(ref_runs, channel="REF")

        # --- Build hw_edges (offline or live capture) ---
        caveats: list[str] = []

        if hw_edges is not None:
            # Offline: edges provided directly.
            captured_edges = [
                edges_helper.Edge(t_us=float(e[0]), channel="HW", kind=str(e[1]))
                for e in hw_edges
            ]
        elif hw_runs is not None:
            # Offline: runs provided, convert to edges.
            cap_runs = [
                rle_helper.Run(t_us=float(r[0]), level=int(r[1]), dur_us=float(r[2]))
                for r in hw_runs
            ]
            captured_edges = refdiff_helper.runs_to_edges(cap_runs, channel="HW")
        else:
            # Live capture from scope.
            if hysteresis_v < 0:
                raise ValueError("'hysteresis_v' must be >= 0")
            try:
                scope = open_scope(
                    host=host, port=port, model=model, timeout_s=timeout_s
                )
                wf = scope.read_waveform(source)
            except ScopeDispatchError as e:
                raise RuntimeError(f"scope dispatch failed: {e}") from e
            except ScpiError as e:
                raise RuntimeError(f"SCPI transport error: {e}") from e

            levels = quantize_helper.quantize(wf.volts, threshold_v, hysteresis_v)
            runs = rle_helper.runs_from_levels(
                levels, t0_us=wf.t0_s * 1e6, dt_us=wf.dt_s * 1e6
            )
            truncated = len(runs) > _MAX_RUNS
            if truncated:
                runs = runs[:_MAX_RUNS]
            captured_edges = edges_helper.edges_from_runs(runs, wf.source)

            caveats.extend(
                caveat_for_waveform_quantize(
                    hysteresis_v=hysteresis_v,
                    peak_to_peak_v=quantize_helper.peak_to_peak(wf.volts),
                    truncated=truncated,
                )
            )

        # --- Run the diff ---
        diff = refdiff_helper.reference_diff(ref_edges, captured_edges, tolerance_us)

        diff["caveats"] = caveats
        return diff

    @mcp.tool(
        name="scope_viewer",
        description=(
            "Capture waveform data and generate a self-contained interactive "
            "HTML viewer file. The viewer displays analog waveforms with "
            "zoom/pan, per-channel V/div and ON/OFF controls, draggable GND "
            "offsets, a T/div selector, and a cursor with voltage readout. "
            "Data is embedded without downsampling. "
            "'depth' controls memory depth (and thus observation window "
            "duration at the same sample rate): 'low' (~30k pts/ch, fast), "
            "'mid' (~300k, standard), 'high' (~3M+, long captures). "
            "The scope must be STOPPED before calling (use "
            "scope_trigger(action='STOP') or a completed SINGLE capture). "
            "Returns the path and file size of the generated HTML."
        ),
    )
    def scope_viewer(
        output_path: str,
        channels: list[str] | None = None,
        depth: str = "mid",
        title: str = "Oscilloscope Capture",
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        from pathlib import Path as _Path
        from oscilloscope_mcp.helpers.viewer import generate_viewer_html

        out = _Path(output_path).expanduser()
        if out.exists() and out.is_dir():
            raise ValueError(
                f"'output_path' must be a file, not a directory: {out}"
            )

        # Depth presets (4ch values; 1ch is 4x larger but we use the
        # conservative 4ch values so any channel count works)
        depth_map = {"low": 30000, "mid": 300000, "high": 3000000}
        depth_key = depth.lower()
        if depth_key not in depth_map:
            raise ValueError(
                f"depth must be 'low', 'mid', or 'high'; got {depth!r}"
            )

        try:
            scope = open_scope(
                host=host, port=port, model=model, timeout_s=timeout_s
            )
            # Verify scope is stopped
            status = scope.get_trigger().status or ""
            if status.upper() not in ("STOP", "TD"):
                raise RuntimeError(
                    f"scope must be stopped for viewer capture "
                    f"(status={status!r}); use "
                    "scope_trigger(action='STOP') first"
                )

            # Determine channels
            if channels is None:
                ch_count = scope.active_channel_count()
                channels = [
                    f"CHAN{i}" for i in range(1, ch_count + 1)
                    if scope.transport.query(
                        f":CHAN{i}:DISP?"
                    ).strip() in ("1", "ON")
                ]
            if not channels:
                channels = ["CHAN1"]

            # Depth limits: trim to max_pts centered around trigger so
            # the viewer shows pre+post trigger symmetrically.
            max_pts = depth_map[depth_key]

            # Read waveforms (RAW mode for full data, then trim around trigger)
            colors_default = {
                "CHAN1": "#ffca28", "CHAN2": "#4fc3f7",
                "CHAN3": "#ff4081", "CHAN4": "#2979ff",
            }
            channels_data = {}
            for ch in channels:
                wf = scope.read_waveform(ch, mode="RAW")
                ch_num = int(ch[-1])
                scale = scope.channel_scale_v_per_div(ch_num)
                offset = scope.channel_offset_v(ch_num)
                # Trim centered on trigger (t=0). Since read_waveform
                # already offsets t0_s so trigger=0, find the sample
                # index where t=0 falls.
                if wf.n_samples > max_pts and wf.dt_s > 0:
                    trig_idx = int(-wf.t0_s / wf.dt_s)
                    half = max_pts // 2
                    start = max(0, trig_idx - half)
                    end = min(wf.n_samples, start + max_pts)
                    start = max(0, end - max_pts)
                    volts = wf.volts[start:end]
                    t0_us = (wf.t0_s + start * wf.dt_s) * 1e6
                else:
                    volts = wf.volts
                    t0_us = wf.t0_s * 1e6
                channels_data[ch] = {
                    "volts": [round(v, 3) for v in volts],
                    "t0_us": t0_us,
                    "dt_us": wf.dt_s * 1e6,
                    "n_samples": len(volts),
                    "scale_v_per_div": scale,
                    "offset_v": offset,
                }

        except ScopeDispatchError as e:
            raise RuntimeError(f"scope dispatch failed: {e}") from e
        except ScpiError as e:
            raise RuntimeError(f"SCPI transport error: {e}") from e

        colors = {ch: colors_default.get(ch, "#66bb6a") for ch in channels}
        html = generate_viewer_html(channels_data, colors=colors, title=title)

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")

        return {
            "path": str(out),
            "bytes_written": len(html.encode("utf-8")),
            "channels": channels,
            "depth": depth_key,
            "points_per_channel": {
                ch: d["n_samples"] for ch, d in channels_data.items()
            },
        }

    return mcp


def _parse_cursor(spec: dict[str, Any]) -> CursorPair:
    try:
        return CursorPair(
            label=str(spec.get("label", "")),
            source_channel=int(spec["source_channel"]),
            ax_t_s=_opt_float(spec, "ax_t_s"),
            bx_t_s=_opt_float(spec, "bx_t_s"),
            ay_v=_opt_float(spec, "ay_v"),
            by_v=_opt_float(spec, "by_v"),
        )
    except KeyError as e:
        raise ValueError(f"cursor pair missing field: {e.args[0]!r}") from e


def _opt_float(spec: dict[str, Any], key: str) -> float | None:
    val = spec.get(key)
    return None if val is None else float(val)


def _cursor_to_dict(pair: CursorPair) -> dict[str, Any]:
    d: dict[str, Any] = {
        "label": pair.label,
        "source_channel": pair.source_channel,
    }
    for k in ("ax_t_s", "bx_t_s", "ay_v", "by_v"):
        v = getattr(pair, k)
        if v is not None:
            d[k] = v
    if pair.ax_t_s is not None and pair.bx_t_s is not None:
        d["delta_t_s"] = pair.bx_t_s - pair.ax_t_s
    if pair.ay_v is not None and pair.by_v is not None:
        d["delta_v"] = pair.by_v - pair.ay_v
    return d
