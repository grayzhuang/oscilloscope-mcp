"""Capability profile + current setting → observation-limit caveats.

The agent reads structured fields ("CH1 pulse width = 1.64 μs") and
acts on them. If a measurement was taken outside the instrument's safe
range, that fact must be visible alongside the number; otherwise the
agent will trust an artefact. This module computes the warning list.

The set of rules grows with phasing (P1 ships only a handful below).
Each rule is small and pure so that adding a rule = adding a function +
one entry to :func:`compute_caveats`.
"""

from __future__ import annotations

from typing import Any


def compute_caveats(
    profile: dict[str, Any],
    *,
    active_channel_count: int,
    timebase_s_per_div: float | None = None,
    expected_signal_freq_hz: float | None = None,
) -> list[str]:
    """Compute caveats for a single capture invocation.

    Parameters
    ----------
    profile : dict
        Loaded YAML profile (``bench/instruments/profiles/<model>.yaml``).
    active_channel_count : int
        Number of channels currently displayed. Many scopes drop the
        per-channel sample rate as more channels turn on.
    timebase_s_per_div : float or None
        Current main-timebase setting. Used to estimate trigger jitter
        and memory-depth limits when known.
    expected_signal_freq_hz : float or None
        Caller-declared signal frequency. If above the analog bandwidth
        or above a practical fraction of the sample rate, a caveat is
        emitted.

    Returns
    -------
    list[str]
        Empty if all checks pass. Agents are contractually required to
        consult this before trusting numeric outputs.
    """
    caveats: list[str] = []

    sample_rate = _sample_rate_for_channels(profile, active_channel_count)
    if sample_rate is not None and active_channel_count > 1:
        # Most multi-channel scopes share an ADC pool. Note the downgrade
        # explicitly so the agent doesn't quote the spec-sheet maximum.
        max_rate = _max_sample_rate(profile)
        if max_rate is not None and sample_rate < max_rate:
            caveats.append(
                f"{active_channel_count}-channel mode active → sample rate "
                f"= {_fmt_sa(sample_rate)} (max {_fmt_sa(max_rate)} at 1ch); "
                f"practical observation limit ≈ {_fmt_hz(sample_rate / 5)} "
                f"(1/5 Nyquist rule of thumb)"
            )

    analog_bw = _get_capability(profile, "analog_bw_hz")
    if expected_signal_freq_hz is not None and analog_bw is not None:
        if expected_signal_freq_hz > analog_bw:
            caveats.append(
                f"expected signal frequency {_fmt_hz(expected_signal_freq_hz)} "
                f"exceeds scope analog BW {_fmt_hz(analog_bw)}; "
                "observed waveform amplitude/edges will be attenuated and distorted"
            )

    if sample_rate is not None and expected_signal_freq_hz is not None:
        practical_limit = sample_rate / 5.0
        if expected_signal_freq_hz > practical_limit:
            caveats.append(
                f"expected signal frequency {_fmt_hz(expected_signal_freq_hz)} "
                f"exceeds practical observation limit "
                f"{_fmt_hz(practical_limit)} for current sample rate; "
                "edge timing < 5 samples per period — not trustworthy"
            )

    if sample_rate is not None and timebase_s_per_div is not None:
        # DS1000Z displays 12 divisions, so total window = 12 × s/div.
        window_s = 12.0 * timebase_s_per_div
        required_pts = window_s * sample_rate
        mem_pts = _get_capability(profile, "memory_depth_pts_total")
        # Allocate evenly across active channels when multi-channel mode
        # divides the memory pool (typical on DS1000Z).
        if mem_pts is not None and active_channel_count > 0:
            per_ch_mem = mem_pts / active_channel_count
            if required_pts > per_ch_mem:
                effective = per_ch_mem / window_s
                caveats.append(
                    f"timebase {_fmt_time(timebase_s_per_div)}/div × "
                    f"{_fmt_sa(sample_rate)} requires "
                    f"{required_pts:.0f} pts but per-channel memory "
                    f"= {per_ch_mem:.0f} pts ({active_channel_count}-ch); "
                    f"sample rate effectively downgraded to {_fmt_sa(effective)}"
                )

    return caveats


def caveat_for_raw_scpi() -> list[str]:
    """Caveat attached to ``bench_scope_query`` (raw SCPI passthrough).

    Raw passthrough bypasses the typed setup → capability → caveat path,
    so the result is unannotated by definition. The tool emits this so
    the agent does not silently trust the response.
    """
    return [
        "raw SCPI passthrough — instrument-limit checks (sample rate, "
        "bandwidth, memory depth) are not applied. Consult the model "
        "profile (bench/instruments/profiles/) before quoting numeric "
        "values."
    ]


def caveat_for_unmeasurable_items(items: list[str]) -> list[str]:
    """Caveat attached to ``scope_measure`` when one or more requested
    items came back as the instrument's un-measurable sentinel (~9.9e37).

    The agent receives ``None`` for those items in ``measurements``; this
    caveat names them explicitly so the agent does not mistake a missing
    value for zero (e.g. FREQUENCY on a flat / un-triggered trace).
    """
    if not items:
        return []
    return [
        "the following requested measurement(s) are currently "
        f"un-measurable on this source and returned null: {', '.join(items)}. "
        "Common causes: no stable trigger, the waveform is flat / off-screen, "
        "or the item does not apply to this signal."
    ]


def caveat_for_waveform_quantize(
    *,
    hysteresis_v: float,
    peak_to_peak_v: float,
    truncated: bool = False,
) -> list[str]:
    """Caveats attached to ``scope_waveform`` (quantize → RLE event stream).

    Two rules (ROADMAP P1.5):

    - **detection-miss** — if the hysteresis band is wider than 20 % of
      the signal peak-to-peak amplitude, real transitions whose swing
      is smaller than the band will be silently *missed* (the comparator
      never crosses both thresholds). Surface this so the agent does not
      conclude "no edge" when an edge was simply below the dead band.
    - **truncated** — if the run/edge stream was clipped to keep the JSON
      under budget, downstream timing past the cut-off is incomplete.
    """
    caveats: list[str] = []
    if peak_to_peak_v > 0 and hysteresis_v > 0.20 * peak_to_peak_v:
        caveats.append(
            f"hysteresis {hysteresis_v:g} V exceeds 20% of signal "
            f"peak-to-peak ({peak_to_peak_v:g} V) — transitions with a "
            "swing smaller than the hysteresis band are missed (no edge "
            "emitted). Lower hysteresis_v or check the threshold."
        )
    elif peak_to_peak_v == 0:
        caveats.append(
            "signal is flat (peak-to-peak = 0 V over the captured window); "
            "no transitions can be detected — check the channel, probe, and "
            "timebase."
        )
    if truncated:
        caveats.append(
            "run/edge stream was truncated to keep the response under the "
            "size budget; timing beyond the cut-off is not reported. Narrow "
            "the timebase or capture a shorter window for full coverage."
        )
    return caveats


def caveat_for_cursor_readout() -> list[str]:
    """Caveat attached to cursor-position read-outs.

    Cursor coordinates are bound by display pixel resolution and are
    less precise than ``:MEAS:ITEM?`` queries. The screenshot tool emits
    this whenever cursor positions are exposed in its result.
    """
    return [
        "cursor read-out is limited to display pixel resolution; for "
        "precise Δt or ΔV, use a measurement query (:MEAS:ITEM?) rather "
        "than reading cursor coordinates"
    ]


def caveat_for_bw_limit(profile: dict[str, Any], bw_limit: str) -> list[str]:
    """Caveat attached when a channel's hardware bandwidth limit is engaged.

    A 20 MHz BW limit attenuates everything above the cutoff, so edge
    timing and high-frequency content read back distorted — the agent must
    not trust fast-edge numbers from a band-limited channel.
    """
    s = (bw_limit or "").strip().upper()
    if s in ("", "OFF"):
        return []
    cutoff = "20 MHz" if s in ("20M", "20MHZ") else s
    return [
        f"channel bandwidth limit {cutoff} is engaged — content above "
        f"{cutoff} is attenuated; edge/rise-time and high-frequency "
        "amplitude read back distorted. Set bw_limit OFF for fast nodes."
    ]


def caveat_for_timebase(
    profile: dict[str, Any],
    *,
    active_channel_count: int,
    timebase_s_per_div: float,
) -> list[str]:
    """Observation-limit caveats implied by a timebase setting.

    Thin wrapper over :func:`compute_caveats` that reuses the memory-depth
    rule: a long timebase × the current sample rate can exceed per-channel
    memory and silently downgrade the effective sample rate. Emitted by
    ``scope_timebase`` when a new s/div is applied.
    """
    return compute_caveats(
        profile,
        active_channel_count=active_channel_count,
        timebase_s_per_div=timebase_s_per_div,
    )


# ---------------------------------------------------------------------------
# Profile accessors
# ---------------------------------------------------------------------------


def _get_capability(profile: dict[str, Any], key: str) -> float | None:
    cap = profile.get("capability") or {}
    val = cap.get(key)
    return float(val) if val is not None else None


def _sample_rate_for_channels(
    profile: dict[str, Any], channel_count: int
) -> float | None:
    cap = profile.get("capability") or {}
    table = cap.get("sample_rate_mode_dependent")
    if not table:
        return _max_sample_rate(profile)
    # Use the exact key if present; otherwise pick the slowest entry whose
    # channel-count threshold is <= active count (degrades monotonically).
    key = str(channel_count)
    if key in table:
        return float(table[key])
    candidates = sorted(
        (int(k), float(v)) for k, v in table.items() if k.isdigit()
    )
    pick: float | None = None
    for k, v in candidates:
        if k <= channel_count:
            pick = v
    return pick


def _max_sample_rate(profile: dict[str, Any]) -> float | None:
    cap = profile.get("capability") or {}
    table = cap.get("sample_rate_mode_dependent")
    if table:
        return max(float(v) for v in table.values())
    return _get_capability(profile, "sample_rate_max_sa_s")


# ---------------------------------------------------------------------------
# Number formatting (compact, agent-readable)
# ---------------------------------------------------------------------------


def _fmt_sa(rate: float) -> str:
    for unit, scale in (("GSa/s", 1e9), ("MSa/s", 1e6), ("kSa/s", 1e3)):
        if rate >= scale:
            return f"{rate / scale:.3g} {unit}"
    return f"{rate:.0f} Sa/s"


def _fmt_hz(freq: float) -> str:
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if freq >= scale:
            return f"{freq / scale:.3g} {unit}"
    return f"{freq:.0f} Hz"


def _fmt_time(s: float) -> str:
    for unit, scale in (("s", 1.0), ("ms", 1e-3), ("μs", 1e-6), ("ns", 1e-9)):
        if s >= scale:
            return f"{s / scale:.3g} {unit}"
    return f"{s:.3g} s"
