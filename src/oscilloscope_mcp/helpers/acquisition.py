"""Acquisition-setup profile interpretation — validation / normalization.

The capability profile declares the allowed vertical (channel) and
horizontal (timebase) setup values in the ``capability.acquisition``
block (shared across the DS1000Z family via ``_ds1000z_family.yaml``).
This module is the single place that reads that block, so the driver and
the ``scope_channel`` / ``scope_timebase`` MCP tools never hard-code the
enum lists or numeric ranges — they mirror the role :mod:`.trigger`
plays for the trigger subsystem.

Everything here is a pure function over the profile dict. Validation
errors are :class:`AcquisitionValidationError` (a :class:`ValueError`
subclass) so the MCP layer surfaces them as clean tool errors and they
are raised *before* any SCPI is sent (rejected, not mis-applied).
"""

from __future__ import annotations

import re
from typing import Any


class AcquisitionValidationError(ValueError):
    """Raised when a requested channel / timebase value is not declared in
    the model profile, or is out of the profile-declared range. Subclass
    of :class:`ValueError` so the MCP layer surfaces it as a clean tool
    error.
    """


# ---------------------------------------------------------------------------
# Profile accessors
# ---------------------------------------------------------------------------


def _acquisition_block(profile: dict[str, Any]) -> dict[str, Any]:
    cap = profile.get("capability") or {}
    acq = cap.get("acquisition")
    if not acq:
        model = profile.get("model", "<unknown>")
        raise AcquisitionValidationError(
            f"profile for {model} declares no capability.acquisition; "
            "cannot validate channel / timebase operations"
        )
    return acq


def _channel_block(profile: dict[str, Any]) -> dict[str, Any]:
    return dict(_acquisition_block(profile).get("channel") or {})


def _timebase_block(profile: dict[str, Any]) -> dict[str, Any]:
    return dict(_acquisition_block(profile).get("timebase") or {})


def channel_count(profile: dict[str, Any]) -> int:
    return int((profile.get("capability") or {}).get("channels", 4))


# ---------------------------------------------------------------------------
# Channel validation / normalization
# ---------------------------------------------------------------------------


def validate_channel_number(profile: dict[str, Any], channel: Any) -> int:
    """Coerce ``channel`` to an int bounded by ``capability.channels``.

    Accepts ``1``, ``"1"``, or ``"CHAN1"`` / ``"CHANnel1"`` (case-
    insensitive). Raises :class:`AcquisitionValidationError` if it is not
    a whole number in ``1..channels``.
    """
    n = channel
    if isinstance(channel, str):
        m = re.fullmatch(r"(?:CHAN(?:NEL)?)?([0-9]+)", channel.strip().upper())
        if not m:
            raise AcquisitionValidationError(
                f"channel {channel!r} must be an integer or 'CHAN<n>'"
            )
        n = int(m.group(1))
    else:
        try:
            fn = float(channel)
        except (TypeError, ValueError):
            raise AcquisitionValidationError(
                f"channel {channel!r} must be an integer"
            )
        if fn != int(fn):
            raise AcquisitionValidationError(
                f"channel {channel!r} must be a whole number"
            )
        n = int(fn)
    ch = channel_count(profile)
    if not 1 <= n <= ch:
        raise AcquisitionValidationError(
            f"channel {n} out of range (1..{ch})"
        )
    return n


def validate_coupling(profile: dict[str, Any], coupling: str) -> str:
    """Validate channel coupling against the profile; return the canonical
    upper-case form (``AC`` / ``DC`` / ``GND``).
    """
    allowed = [str(c) for c in _channel_block(profile).get("coupling_modes", [])]
    want = str(coupling).strip().upper()
    for entry in allowed:
        if want == str(entry).upper():
            return str(entry).upper()
    raise AcquisitionValidationError(
        f"coupling {coupling!r} invalid; valid: {', '.join(allowed)}"
    )


def validate_units(profile: dict[str, Any], units: str) -> str:
    """Validate channel units against the profile; return the canonical
    long-form keyword as the profile spells it (e.g. ``VOLTage``).

    The short form (``VOLT``) and any case variant are accepted.
    """
    allowed = [str(u) for u in _channel_block(profile).get("units", [])]
    want = str(units).strip().upper()
    for entry in allowed:
        if want == str(entry).upper() or want == _short_form(str(entry)):
            return str(entry)
    raise AcquisitionValidationError(
        f"units {units!r} invalid; valid: {', '.join(allowed)}"
    )


def validate_bw_limit(profile: dict[str, Any], bw_limit: str) -> str:
    """Validate a bandwidth-limit selection against the profile; return
    the canonical form the instrument expects (``20M`` / ``OFF``).
    """
    allowed = [str(b) for b in _channel_block(profile).get("bw_limit_values", [])]
    want = str(bw_limit).strip().upper()
    for entry in allowed:
        if want == str(entry).upper():
            return str(entry).upper()
    raise AcquisitionValidationError(
        f"bw_limit {bw_limit!r} invalid; valid: {', '.join(allowed)}"
    )


def validate_probe(profile: dict[str, Any], probe: Any) -> float:
    """Validate a probe attenuation ratio against the profile's discrete
    list; return the matched ratio as a float (the value the instrument
    accepts, formatted by the driver).
    """
    ratios = [float(r) for r in _channel_block(profile).get("probe_ratios", [])]
    try:
        want = float(probe)
    except (TypeError, ValueError):
        raise AcquisitionValidationError(f"probe {probe!r} must be a number")
    for r in ratios:
        # Float compare with a relative tolerance (0.1 vs 0.10000001).
        if abs(r - want) <= 1e-9 + 1e-6 * abs(r):
            return r
    valid = ", ".join(_fmt_num(r) for r in ratios)
    raise AcquisitionValidationError(
        f"probe ratio {probe!r} invalid; valid: {valid}"
    )


def validate_display(display: Any) -> bool:
    """Coerce a display on/off request to a bool. Accepts bool, ``1``/``0``,
    or ``ON``/``OFF`` (any case)."""
    return _coerce_bool("display", display)


def validate_invert(invert: Any) -> bool:
    """Coerce an invert on/off request to a bool (see :func:`validate_display`)."""
    return _coerce_bool("invert", invert)


def validate_channel_scale(profile: dict[str, Any], scale_v_per_div: Any) -> float:
    """Range-check a vertical scale (V/div) against the profile."""
    return _range_check_real(
        "scale_v_per_div", _channel_block(profile).get("scale_v_per_div"),
        scale_v_per_div, positive=True,
    )


def validate_channel_offset(profile: dict[str, Any], offset_v: Any) -> float:
    """Range-check a vertical offset (V) against the profile."""
    return _range_check_real(
        "offset_v", _channel_block(profile).get("offset_v"), offset_v,
    )


# ---------------------------------------------------------------------------
# Acquire validation / normalization
# ---------------------------------------------------------------------------


def _acquire_block(profile: dict[str, Any]) -> dict[str, Any]:
    return dict(_acquisition_block(profile).get("acquire") or {})


def validate_acquire_type(profile: dict[str, Any], acq_type: str) -> str:
    """Validate an acquisition type against the profile; return the canonical
    keyword as the profile spells it (e.g. ``NORMal``, ``AVERages``).

    Accepts case-insensitive match or RIGOL short-form (NORM, AVER, etc.).
    """
    allowed = [str(t) for t in _acquire_block(profile).get("types", [])]
    want = str(acq_type).strip().upper()
    for entry in allowed:
        if want == str(entry).upper() or want == _short_form(str(entry)):
            return str(entry)
    raise AcquisitionValidationError(
        f"acquire type {acq_type!r} invalid; valid: {', '.join(allowed)}"
    )


def validate_averages(profile: dict[str, Any], averages: Any) -> int:
    """Validate an averaging count against the profile's discrete list;
    return the matched count as an int.
    """
    allowed = [int(a) for a in _acquire_block(profile).get("averages", [])]
    try:
        want = int(averages)
    except (TypeError, ValueError):
        raise AcquisitionValidationError(f"averages {averages!r} must be an integer")
    if want in allowed:
        return want
    valid = ", ".join(str(a) for a in allowed)
    raise AcquisitionValidationError(
        f"averages {averages!r} invalid; valid: {valid}"
    )


def validate_memory_depth(
    profile: dict[str, Any], active_channels: int, value: Any
) -> str | int:
    """Validate a memory depth against the profile for the given active
    channel count. Returns the canonical value (``"AUTO"`` or an int).

    The profile declares allowed values per channel count (1/2/4). For 3
    active channels the 4-channel limits apply.
    """
    mdepth_table = _acquire_block(profile).get("memory_depth", {})
    # Pick the right bucket: use exact match or next higher key.
    key = str(active_channels) if str(active_channels) in mdepth_table else "4"
    if active_channels <= 1 and "1" in mdepth_table:
        key = "1"
    elif active_channels <= 2 and "2" in mdepth_table:
        key = "2"
    else:
        key = "4"
    allowed_raw = mdepth_table.get(key, [])
    allowed = [str(v) for v in allowed_raw]

    want = str(value).strip().upper()
    if want == "AUTO":
        if "AUTO" in [str(a).upper() for a in allowed_raw]:
            return "AUTO"
        raise AcquisitionValidationError(
            f"memory_depth 'AUTO' not valid for {active_channels} active channel(s); "
            f"valid: {', '.join(allowed)}"
        )
    # Numeric match.
    try:
        want_int = int(float(want))
    except (TypeError, ValueError):
        raise AcquisitionValidationError(
            f"memory_depth {value!r} must be 'AUTO' or a numeric value"
        )
    for a in allowed_raw:
        if str(a).upper() == "AUTO":
            continue
        if int(float(str(a))) == want_int:
            return want_int
    valid = ", ".join(allowed)
    raise AcquisitionValidationError(
        f"memory_depth {value!r} invalid for {active_channels} active channel(s); "
        f"valid: {valid}"
    )


# ---------------------------------------------------------------------------
# Timebase validation / normalization
# ---------------------------------------------------------------------------


def validate_timebase_mode(profile: dict[str, Any], mode: str) -> str:
    """Validate a timebase mode against the profile; return the canonical
    keyword as the profile spells it (``MAIN`` / ``XY`` / ``ROLL``).
    """
    allowed = [str(m) for m in _timebase_block(profile).get("modes", [])]
    want = str(mode).strip().upper()
    for entry in allowed:
        if want == str(entry).upper() or want == _short_form(str(entry)):
            return str(entry)
    raise AcquisitionValidationError(
        f"timebase mode {mode!r} invalid; valid: {', '.join(allowed)}"
    )


def validate_timebase_scale(profile: dict[str, Any], s_per_div: Any) -> float:
    """Range-check a timebase scale (s/div) against the profile."""
    return _range_check_real(
        "s_per_div", _timebase_block(profile).get("scale_s_per_div"),
        s_per_div, positive=True,
    )


def validate_timebase_offset(profile: dict[str, Any], offset_s: Any) -> float:
    """Range-check a timebase offset (s) against the profile."""
    return _range_check_real(
        "offset_s", _timebase_block(profile).get("offset_s"), offset_s,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _short_form(v: str) -> str:
    """RIGOL keyword short form = its capital letters / digits (VOLTage→VOLT)."""
    return "".join(c for c in v if c.isupper() or c.isdigit())


def _coerce_bool(name: str, value: Any) -> bool:
    if value is True or value is False:
        return bool(value)
    s = str(value).strip().upper()
    if s in {"1", "ON", "TRUE", "YES"}:
        return True
    if s in {"0", "OFF", "FALSE", "NO"}:
        return False
    raise AcquisitionValidationError(
        f"{name}={value!r} must be a boolean (ON/OFF)"
    )


def _range_check_real(
    name: str, bounds: Any, value: Any, *, positive: bool = False
) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise AcquisitionValidationError(f"{name}={value!r} must be a number")
    if positive and x <= 0:
        raise AcquisitionValidationError(f"{name}={value} must be positive")
    bounds = bounds or {}
    lo, hi = bounds.get("min"), bounds.get("max")
    if lo is not None and x < float(lo):
        raise AcquisitionValidationError(f"{name}={value} below minimum {_fmt_num(lo)}")
    if hi is not None and x > float(hi):
        raise AcquisitionValidationError(f"{name}={value} above maximum {_fmt_num(hi)}")
    return x


def _fmt_num(v: Any) -> str:
    """Compact numeric formatting that keeps integers integer-looking
    (1 not 1.0) and uses %g for the rest."""
    f = float(v)
    return str(int(f)) if f == int(f) and abs(f) < 1e15 else f"{f:g}"
