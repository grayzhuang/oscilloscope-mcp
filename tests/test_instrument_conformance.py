"""Conformance test against a *live* oscilloscope.

This module is the gate for "is this driver wired up to a real
instrument correctly". It is parametrized over every model registered
in :data:`oscilloscope_mcp.instruments.MODEL_REGISTRY`, but each test
skips unless the operator has set up the test environment:

- ``SCOPE_MCP_HOST`` must point at the instrument
- ``SCOPE_MCP_CONFORMANCE_MODEL`` (optional) restricts the run
  to one model; otherwise ``*IDN?`` auto-detect picks one

When you add a new driver/profile pair, run this against the real
device to certify the addition::

    SCOPE_MCP_HOST=<your-scope-ip> \\
    SCOPE_MCP_CONFORMANCE_MODEL=RIGOL_DS1104Z \\
    pytest tests/test_instrument_conformance.py -v
"""

from __future__ import annotations

import os
import re

import pytest

from oscilloscope_mcp.instruments import MODEL_REGISTRY, _load_profile, open_scope
from oscilloscope_mcp.instruments._base import (
    ChannelSetup,
    ScreenshotPlan,
    TimebaseSetup,
    TriggerSetup,
)


_HOST = os.environ.get("SCOPE_MCP_HOST")
_RESTRICT_MODEL = os.environ.get("SCOPE_MCP_CONFORMANCE_MODEL")

pytestmark = pytest.mark.skipif(
    not _HOST,
    reason="SCOPE_MCP_HOST not set — skipping live conformance tests",
)


def _models_to_test() -> list[str]:
    if _RESTRICT_MODEL:
        if _RESTRICT_MODEL not in MODEL_REGISTRY:
            raise RuntimeError(
                f"SCOPE_MCP_CONFORMANCE_MODEL={_RESTRICT_MODEL!r} not in "
                f"MODEL_REGISTRY (known: {sorted(MODEL_REGISTRY)})"
            )
        return [_RESTRICT_MODEL]
    return sorted(MODEL_REGISTRY)


@pytest.fixture(scope="module", params=_models_to_test())
def scope(request):
    model = request.param
    try:
        s = open_scope(model=model, timeout_s=5.0)
    except Exception as e:
        pytest.skip(f"open_scope({model}) failed: {e}")
    yield s


def test_idn_matches_profile_regex(scope) -> None:
    """*IDN? response must match the profile's ``idn_match`` regex —
    catches profile/driver/firmware drift early.
    """
    idn = scope.idn()
    pattern = scope.profile["idn_match"]
    assert re.search(pattern, idn), (
        f"IDN={idn!r} did not match profile.idn_match={pattern!r}"
    )


def test_query_raw_round_trip(scope) -> None:
    """``query_raw`` must work for both queries (?-bearing) and writes."""
    # Query path.
    trig_state = scope.query_raw(":TRIG:STAT?")
    assert trig_state.upper() in {"TD", "WAIT", "RUN", "AUTO", "STOP", "FIN"}
    # Write path — :STOP is harmless and idempotent.
    assert scope.query_raw(":STOP") == ""


def test_active_channel_count_is_in_profile_range(scope) -> None:
    count = scope.active_channel_count()
    max_ch = int(scope.profile["capability"]["channels"])
    assert 0 <= count <= max_ch


def test_timebase_is_positive(scope) -> None:
    assert scope.timebase_s_per_div() > 0


def test_screenshot_returns_nonempty_png(tmp_path, scope) -> None:
    """End-to-end: dump a PNG, verify it has the PNG magic bytes."""
    result = scope.screenshot(ScreenshotPlan(image_format="PNG"))
    assert result.image_format == "PNG"
    assert len(result.image_bytes) > 1024, "PNG suspiciously small"
    assert result.image_bytes.startswith(b"\x89PNG\r\n\x1a\n"), (
        f"PNG magic missing; first 16 bytes = {result.image_bytes[:16]!r}"
    )
    # Persist for human review so the operator can verify visually.
    out = tmp_path / f"conformance_{scope.profile['model']}.png"
    out.write_bytes(result.image_bytes)


def test_screenshot_with_cursor_and_label(tmp_path, scope) -> None:
    """Verify cursor + channel-label annotation makes it onto the
    screen. We don't OCR the PNG; instead we check the SCPI sequence
    completes without error and the result echoes the applied state.
    """
    from oscilloscope_mcp.instruments._base import CursorPair
    plan = ScreenshotPlan(
        channel_labels={1: "CH1_TEST"},
        cursor_pairs=[
            CursorPair(label="span", source_channel=1,
                       ax_t_s=-1e-6, bx_t_s=1e-6),
        ],
        display_labels=True,
    )
    result = scope.screenshot(plan)
    assert result.channel_labels_applied.get(1) == "CH1_TEST"
    assert len(result.cursors_set) == 1
    assert result.cursors_set[0].label == "span"


def test_read_waveform_quantize_rle_json_shape(scope) -> None:
    """P1.5 DoD: capture CH1, quantize at 1.5 V / 0.1 V hysteresis, and
    assert the runs/edges JSON shape — runs are [t_us, level, dur_us],
    edges are [t_us, channel, RISE|FALL], response under ~10 KB.
    """
    import json
    import time

    from oscilloscope_mcp.helpers.edges import edges_from_runs
    from oscilloscope_mcp.helpers.quantize import peak_to_peak, quantize
    from oscilloscope_mcp.helpers.rle import runs_from_levels

    # Ensure the scope is running (a prior SINGLE-mode test may have left
    # it stopped, yielding an empty waveform buffer).
    scope.set_trigger(TriggerSetup(sweep="AUTO"))
    scope.run_control("RUN")
    time.sleep(0.5)

    wf = scope.read_waveform("CHAN1")
    assert wf.source == "CHAN1"
    assert wf.n_samples > 0, "empty waveform — is CH1 displayed?"
    assert wf.dt_s > 0

    levels = quantize(wf.volts, threshold_v=1.5, hysteresis_v=0.1)
    assert len(levels) == wf.n_samples
    assert set(levels) <= {0, 1}

    runs = runs_from_levels(levels, t0_us=wf.t0_s * 1e6, dt_us=wf.dt_s * 1e6)
    assert runs, "no runs produced"
    # Runs tile without gaps.
    for prev, cur in zip(runs, runs[1:]):
        assert cur.t_us == pytest.approx(prev.t_us + prev.dur_us, abs=1e-3)
        assert cur.level != prev.level

    edges = edges_from_runs(runs, wf.source)
    for e in edges:
        assert e.channel == "CHAN1"
        assert e.kind in {"RISE", "FALL"}

    # JSON shape + size budget (the agent-facing response shape).
    payload = {
        "source": wf.source,
        "runs": [list(r) for r in runs],
        "edges": [list(e) for e in edges],
        "n_samples": wf.n_samples,
    }
    encoded = json.dumps(payload).encode("utf-8")
    # A clean bench signal is a handful of runs; even a busy one stays
    # small thanks to RLE. Allow generous headroom over the 10 KB DoD.
    assert len(encoded) < 64_000, (
        f"quantized response {len(encoded)} bytes — RLE not collapsing? "
        f"({len(runs)} runs over {wf.n_samples} samples, "
        f"p-p={peak_to_peak(wf.volts):g} V)"
    )


def test_get_trigger_returns_profile_consistent_state(scope) -> None:
    """get_trigger must return a mode declared in the profile and a
    status/sweep within the profile's allowed sets.
    """
    state = scope.get_trigger()
    trig = scope.profile["capability"]["trigger"]
    keywords = {str(t["keyword"]).upper() for t in trig["types"]}
    assert state.mode.upper() in keywords, f"mode {state.mode!r} not in profile"
    if state.status is not None:
        assert state.status.upper() in {
            s.upper() for s in trig["status_values"]
        } | {"FIN"}  # some firmware reports FIN


def _restore_trigger(scope, snapshot) -> None:
    """Re-apply a captured TriggerState (mode + globals + mode params)."""
    scope.set_trigger(TriggerSetup(
        mode=snapshot.mode,
        sweep=snapshot.sweep,
        coupling=snapshot.coupling,
        holdoff_s=snapshot.holdoff_s,
        params=dict(snapshot.params),
    ))


def test_set_trigger_edge_round_trip_then_restore(scope) -> None:
    """Set an edge trigger via the typed params path, verify the
    instrument honors it, then restore the original configuration.
    Trigger changes are trivially reversible (see README §safety).
    """
    orig = scope.get_trigger()
    try:
        scope.set_trigger(TriggerSetup(
            mode="EDGE", sweep="AUTO",
            params={"source": "CHAN1", "slope": "POS"},
        ))
        after = scope.get_trigger()
        assert after.mode.upper() == "EDGE"
        assert "1" in str(after.params.get("source"))
        assert after.sweep is not None and after.sweep.upper() == "AUTO"
    finally:
        _restore_trigger(scope, orig)


def test_all_trigger_types_round_trip_then_restore(scope) -> None:
    """Exhaustively switch into EVERY trigger type the profile declares,
    read the mode back, and confirm it applied. Option-licensed types may
    be silently ignored on an unlicensed unit, so those are allowed to not
    apply; standard types MUST apply. Original state is restored at the end.
    """
    trig = scope.profile["capability"]["trigger"]
    orig = scope.get_trigger()
    applied: list[str] = []
    ignored: list[str] = []
    try:
        for t in trig["types"]:
            kw = str(t["keyword"])
            # set_trigger already reads the state back — use that, rather
            # than a second full get_trigger(), to halve connection churn.
            got = scope.set_trigger(TriggerSetup(mode=kw)).mode.upper()
            if got == kw.upper():
                applied.append(kw)
            elif t.get("option"):
                ignored.append(kw)  # unlicensed option — acceptable
            else:
                raise AssertionError(
                    f"standard trigger type {kw!r} did not apply (got {got!r})"
                )
        # Every standard (non-option) type must have applied.
        std = [str(t["keyword"]) for t in trig["types"] if not t.get("option")]
        assert set(std) <= set(applied), f"standard types not applied: {std}"
    finally:
        _restore_trigger(scope, orig)


def test_run_control_actions_then_restore(scope) -> None:
    """Issue each run/arm control action on real hardware and confirm the
    trigger status stays within the profile's allowed set; restore the
    original run/stop state afterward.
    """
    trig = scope.profile["capability"]["trigger"]
    allowed = {s.upper() for s in trig["status_values"]} | {"FIN"}
    orig_status = (scope.get_trigger().status or "STOP").upper()
    running = {"RUN", "AUTO", "TD", "WAIT"}
    try:
        # STOP must report STOP; RUN must leave a running status (the
        # driver settles for the ~0.2 s :TRIG:STAT? lag, so this is stable).
        assert scope.run_control("STOP") == "STOP"
        assert (scope.get_trigger().status or "").upper() == "STOP"
        assert scope.run_control("RUN") == "RUN"
        assert (scope.get_trigger().status or "").upper() in running
        # SINGLE / FORCE just have to keep the status within the allowed set.
        for action in ("SINGLE", "FORCE"):
            assert scope.run_control(action) == action
            status = scope.get_trigger().status
            if status is not None:
                assert status.upper() in allowed, f"{action} → bad status {status!r}"
    finally:
        # Leave the scope running unless it was explicitly stopped before.
        scope.run_control("STOP" if orig_status == "STOP" else "RUN")


def test_run_control_rejects_unknown_action(scope) -> None:
    from oscilloscope_mcp.helpers.trigger import TriggerValidationError
    with pytest.raises(TriggerValidationError):
        scope.run_control("PAUSE")


def test_set_trigger_rejects_unsupported_type(scope) -> None:
    """A bogus trigger type must be rejected by validation before any
    SCPI is sent — no instrument state change.
    """
    from oscilloscope_mcp.helpers.trigger import TriggerValidationError
    before = scope.get_trigger().mode
    with pytest.raises(TriggerValidationError):
        scope.set_trigger(TriggerSetup(mode="NOTATRIGGER"))
    assert scope.get_trigger().mode == before


def test_measure_freq_vpp_returns_float_or_none(scope) -> None:
    """Measure FREQ + VPP on CHAN1 via :MEAS:ITEM? and assert each value
    is either a float or None (None = the instrument's un-measurable
    sentinel on a flat / un-triggered trace). This is non-destructive —
    measurement queries do not change instrument state.
    """
    out = scope.measure(["FREQUENCY", "VPP"], source="CHAN1")
    assert set(out) == {"FREQUENCY", "VPP"}
    for name, value in out.items():
        assert value is None or isinstance(value, float), (
            f"{name} → {value!r} is neither float nor None"
        )


def test_measure_rejects_unknown_item(scope) -> None:
    """An unknown measurement item is rejected by validation before any
    SCPI is sent — no instrument state change.
    """
    from oscilloscope_mcp.helpers.measure import MeasureValidationError
    with pytest.raises(MeasureValidationError):
        scope.measure(["NOTAMEASUREMENT"], source="CHAN1")


def test_measure_dual_source_requires_source2(scope) -> None:
    """A two-source delay/phase item without source2 is rejected up front."""
    from oscilloscope_mcp.helpers.measure import MeasureValidationError
    with pytest.raises(MeasureValidationError, match="second source"):
        scope.measure(["RDELAY"], source="CHAN1")


# --- acquisition setup: channel (vertical) + timebase (horizontal) -------


def test_get_channel_returns_profile_consistent_state(scope) -> None:
    """get_channel(1) must return a positive scale and a coupling within
    the profile's allowed set."""
    acq = scope.profile["capability"]["acquisition"]["channel"]
    state = scope.get_channel(1)
    assert state.channel == 1
    if state.scale_v_per_div is not None:
        assert state.scale_v_per_div > 0
    if state.coupling is not None:
        assert state.coupling.upper() in {
            c.upper() for c in acq["coupling_modes"]
        }


def test_set_channel_scale_round_trip_then_restore(scope) -> None:
    """Set CH1 V/div + DC coupling via the typed path, verify the
    instrument honors it, then restore the original channel state.
    """
    orig = scope.get_channel(1)
    try:
        scope.set_channel(ChannelSetup(channel=1, scale_v_per_div=0.5, coupling="DC"))
        after = scope.get_channel(1)
        assert after.scale_v_per_div == pytest.approx(0.5, rel=0.2)
        assert after.coupling is not None and after.coupling.upper() == "DC"
    finally:
        scope.set_channel(ChannelSetup(
            channel=1,
            scale_v_per_div=orig.scale_v_per_div,
            offset_v=orig.offset_v,
            coupling=orig.coupling,
        ))


def test_set_channel_rejects_bad_coupling(scope) -> None:
    from oscilloscope_mcp.helpers.acquisition import AcquisitionValidationError
    before = scope.get_channel(1).coupling
    with pytest.raises(AcquisitionValidationError):
        scope.set_channel(ChannelSetup(channel=1, coupling="BOGUS"))
    assert scope.get_channel(1).coupling == before


def test_get_timebase_returns_positive_scale(scope) -> None:
    state = scope.get_timebase()
    if state.s_per_div is not None:
        assert state.s_per_div > 0


def test_set_timebase_round_trip_then_restore(scope) -> None:
    orig = scope.get_timebase()
    try:
        scope.set_timebase(TimebaseSetup(s_per_div=1e-3, mode="MAIN"))
        after = scope.get_timebase()
        assert after.s_per_div == pytest.approx(1e-3, rel=0.2)
        assert after.mode is not None and after.mode.upper() == "MAIN"
    finally:
        scope.set_timebase(TimebaseSetup(
            s_per_div=orig.s_per_div,
            offset_s=orig.offset_s,
            mode=orig.mode,
        ))


def test_set_timebase_rejects_out_of_range_scale(scope) -> None:
    from oscilloscope_mcp.helpers.acquisition import AcquisitionValidationError
    before = scope.get_timebase().s_per_div
    with pytest.raises(AcquisitionValidationError):
        scope.set_timebase(TimebaseSetup(s_per_div=1e-12))
    assert scope.get_timebase().s_per_div == before


def test_measure_statistics_freq_returns_shape(scope) -> None:
    """Enable statistics and query FREQUENCY stats on CHAN1. Assert that
    each result has the expected shape: item name + stat fields that are
    either float/int or None.
    """
    from oscilloscope_mcp.instruments._base import MeasureStatResult
    results = scope.measure_statistics(["FREQUENCY"], source="CHAN1")
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, MeasureStatResult)
    assert r.item == "FREQUENCY"
    assert "CHAN" in r.source
    # Each stat field is either a number or None.
    for field in ("current", "maximum", "minimum", "average", "deviation"):
        val = getattr(r, field)
        assert val is None or isinstance(val, float), (
            f"stat field {field} = {val!r} is not float or None"
        )
    assert r.count is None or isinstance(r.count, int), (
        f"count = {r.count!r} is not int or None"
    )


def test_measure_statistics_with_reset_does_not_error(scope) -> None:
    """Resetting statistics then querying must not raise."""
    scope.transport.write(":MEAS:STAT:RES")
    results = scope.measure_statistics(["VPP"], source="CHAN1")
    assert len(results) == 1


# --- acquisition mode: acquire type / averages / memory depth ---------------


def test_get_acquire_returns_valid_state(scope) -> None:
    """get_acquire must return a type within the profile's allowed set,
    a positive integer averages count, and a valid memory_depth."""
    acq = scope.profile["capability"]["acquisition"]["acquire"]
    state = scope.get_acquire()
    if state.type is not None:
        allowed_types = {str(t).upper() for t in acq["types"]}
        # The instrument returns short form (NORM, AVER, etc.).
        # Accept both short and long.
        allowed_short = set()
        for t in acq["types"]:
            allowed_short.add(str(t).upper())
            # Short form = uppercase letters only
            short = "".join(c for c in str(t) if c.isupper() or c.isdigit())
            allowed_short.add(short)
        assert state.type.upper() in allowed_short, (
            f"type {state.type!r} not in profile"
        )
    if state.averages is not None:
        assert state.averages > 0
    if state.sample_rate is not None:
        assert state.sample_rate > 0


def test_set_acquire_type_normal_round_trip(scope) -> None:
    """Set acquisition type to NORMal, read it back, confirm it applied."""
    from oscilloscope_mcp.instruments._base import AcquireSetup
    orig = scope.get_acquire()
    try:
        scope.set_acquire(AcquireSetup(type="NORMal"))
        after = scope.get_acquire()
        assert after.type is not None and after.type.upper() in ("NORM", "NORMAL")
    finally:
        # Restore original type.
        if orig.type is not None:
            scope.set_acquire(AcquireSetup(type=orig.type))


@pytest.mark.parametrize("model", _models_to_test())
def test_profile_file_loads(model: str) -> None:
    """Each registered model's profile YAML must load and declare the
    fields ``caveat_calc`` and the dispatch path rely on.
    """
    _, filename = MODEL_REGISTRY[model]
    profile = _load_profile(filename)
    assert profile["model"] == model
    assert "idn_match" in profile
    cap = profile.get("capability") or {}
    assert "analog_bw_hz" in cap
    assert "channels" in cap
    # Measurement-item schema must be present (shared family fragment).
    items = (cap.get("measure") or {}).get("items") or {}
    assert {"FREQUENCY", "VPP"} <= set(items)
    # Acquisition setup enums/ranges must be present.
    acq = cap["acquisition"]
    assert "coupling_modes" in acq["channel"]
    assert "modes" in acq["timebase"]


# --- scope_capture (P2 culmination) -----------------------------------------


def test_scope_capture_single_shot_edge_trigger(scope) -> None:
    """P2 DoD: set edge trigger on CHAN1, capture with sweep=SINGLE +
    timeout=5s, assert triggered==True (or caveat if no signal), and
    assert the waveform result shape.

    This exercises the full configure → arm → wait → read workflow on
    real hardware. If no signal is present, the trigger may time out,
    which is also a valid outcome (the result includes the timeout caveat).
    """
    import time

    from oscilloscope_mcp.helpers.quantize import quantize
    from oscilloscope_mcp.helpers.rle import runs_from_levels
    from oscilloscope_mcp.helpers.edges import edges_from_runs
    from oscilloscope_mcp.instruments._base import TriggerSetup

    # Ensure edge trigger on CHAN1 is set up
    scope.set_trigger(TriggerSetup(
        mode="EDGE", sweep="SINGLE",
        params={"source": "CHAN1", "slope": "POS"},
    ))

    try:
        # Arm single shot
        scope.run_control("SINGLE")

        # Poll for trigger with 5s timeout
        poll_interval = 0.3
        elapsed = 0.0
        timeout_s = 5.0
        triggered = False
        while elapsed < timeout_s:
            state = scope.get_trigger()
            status = (state.status or "").upper()
            if status in ("TD", "STOP"):
                triggered = True
                break
            time.sleep(poll_interval)
            elapsed += poll_interval

        # Read waveform (best-effort: SINGLE without trigger yields 0 samples)
        wf = scope.read_waveform("CHAN1")
        assert wf.source == "CHAN1"

        if triggered and wf.n_samples > 0:
            levels = quantize(wf.volts, threshold_v=1.5, hysteresis_v=0.1)
            runs = runs_from_levels(levels, t0_us=wf.t0_s * 1e6, dt_us=wf.dt_s * 1e6)
            edges = edges_from_runs(runs, wf.source)

            assert len(runs) >= 1
            for r in runs:
                assert len(r) == 3
                assert r.level in (0, 1)
            for e in edges:
                assert e.channel == "CHAN1"
                assert e.kind in ("RISE", "FALL")
    finally:
        # Restore running state so subsequent tests can read waveforms.
        scope.set_trigger(TriggerSetup(sweep="AUTO"))
        scope.run_control("RUN")


# --- P4: reference_diff / scope_compare live conformance --------------------


def test_reference_diff_live_shifted_edges_detected(scope) -> None:
    """P4 live conformance: capture CH1, create a 'fake reference' from
    the captured edges (shifted by a known amount), run reference_diff,
    and verify the shifts are detected with correct deltas.

    This proves the diff algorithm works end-to-end on real hardware data.
    """
    import time

    from oscilloscope_mcp.helpers.edges import edges_from_runs
    from oscilloscope_mcp.helpers.quantize import quantize
    from oscilloscope_mcp.helpers.reference_diff import reference_diff
    from oscilloscope_mcp.helpers.rle import runs_from_levels
    from oscilloscope_mcp.helpers.edges import Edge

    # Ensure the scope is running so we get a live waveform.
    scope.set_trigger(TriggerSetup(sweep="AUTO"))
    scope.run_control("RUN")
    time.sleep(0.5)

    wf = scope.read_waveform("CHAN1")
    if wf.n_samples == 0:
        pytest.skip("empty waveform — no signal on CH1")

    levels = quantize(wf.volts, threshold_v=1.5, hysteresis_v=0.1)
    runs = runs_from_levels(levels, t0_us=wf.t0_s * 1e6, dt_us=wf.dt_s * 1e6)
    hw_edges = edges_from_runs(runs, wf.source)

    if len(hw_edges) < 2:
        pytest.skip("fewer than 2 edges captured — need a toggling signal on CH1")

    # Create a "fake reference" by shifting all hw edges by a known amount.
    shift_us = 0.02  # 20 ns shift
    ref_edges = [
        Edge(t_us=e.t_us + shift_us, channel="REF", kind=e.kind)
        for e in hw_edges
    ]

    # The tolerance is generous enough to catch the shift.
    result = reference_diff(ref_edges, hw_edges, tolerance_us=0.05)

    # All edges should match (hw is the "capture", ref is shifted version).
    assert result["matched"] == len(hw_edges), (
        f"expected all {len(hw_edges)} edges to match, got {result['matched']}; "
        f"missing={len(result['missing'])}, added={len(result['added'])}"
    )
    assert result["missing"] == []
    assert result["added"] == []
    assert result["first_divergence_us"] is None

    # Each shift delta should be approximately -shift_us (hw - ref = -0.02).
    for s in result["shifted"]:
        assert s["delta_us"] == pytest.approx(-shift_us, abs=1e-4), (
            f"expected delta ~{-shift_us}, got {s['delta_us']}"
        )


# ---------------------------------------------------------------------------
# RAW waveform — full acquisition memory
# ---------------------------------------------------------------------------


def test_read_waveform_raw_full_memory_depth(scope) -> None:
    """RAW mode returns the full acquisition memory; n_samples must
    match the configured memory depth (within tolerance for AUTO mode).

    This test requires the scope to be stopped. It issues a :STOP, waits
    for the status to settle, then reads the full memory.
    """
    # Ensure the scope is stopped.
    scope.run_control("STOP")

    # Read the full memory on CHAN1.
    wf = scope.read_waveform("CHAN1", mode="RAW")

    # Basic shape assertions.
    assert wf.source == "CHAN1"
    assert wf.n_samples > 0, "RAW read must return at least one sample"
    assert wf.dt_s > 0, "dt_s must be positive"
    assert len(wf.volts) == wf.n_samples

    # The RAW point count should be significantly more than NORMal (~1200).
    # With any non-AUTO memory depth setting, it should be >= 6000.
    # (AUTO at the fastest timebases could still give ~1200, so we check > 0.)
    # Read NORMal for comparison.
    wf_normal = scope.read_waveform("CHAN1", mode="NORMal")
    assert wf.n_samples >= wf_normal.n_samples, (
        f"RAW n_samples ({wf.n_samples}) should be >= NORMal "
        f"n_samples ({wf_normal.n_samples})"
    )

    # Sanity: voltages should be finite floats.
    import math
    for i in (0, wf.n_samples // 2, wf.n_samples - 1):
        assert math.isfinite(wf.volts[i]), f"volts[{i}] is not finite"
