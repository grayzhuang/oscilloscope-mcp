"""Measurement profile interpretation — validation, normalization, parsing.

The capability profile declares the *complete* set of automatic
measurement items the instrument supports (``capability.measure.items``
block, shared across the DS1000Z family via ``_ds1000z_family.yaml``).
This module is the single place that reads that block so the driver and
the ``scope_measure`` MCP tool never hard-code the item list — exactly
the way :mod:`oscilloscope_mcp.helpers.trigger` handles trigger types.

Three concerns live here, all pure functions over the profile dict:

1. **Validation** — reject a measurement item the model does not list,
   with an error that enumerates the valid options (so the agent can
   recover without reading the YAML).
2. **Normalization** — the user (or the agent) may pass ``"freq"`` or
   ``"FREQuency"`` or ``"FREQUENCY"``; map any of those to the canonical
   profile entry. A bare channel number (``2``) or ``CHAN2`` is
   normalized to ``CHANnel<n>`` (bounded by ``capability.channels``).
3. **Parsing** — the instrument returns the value in scientific notation,
   or ``~9.9e37`` when the item is currently un-measurable; coerce that
   sentinel to ``None`` so the tool can surface it as a caveat rather
   than a bogus number.
"""

from __future__ import annotations

import re
from typing import Any


class MeasureValidationError(ValueError):
    """Raised when a requested measurement item / source is not declared
    in the model profile. Subclass of :class:`ValueError` so the MCP
    layer surfaces it as a clean tool error (mirrors
    :class:`oscilloscope_mcp.helpers.trigger.TriggerValidationError`).
    """


# The DS1000Z returns this sentinel (~9.9e37) when an item cannot be
# measured on the current waveform. Anything at or above it is "null".
UNMEASURABLE_SENTINEL = 9.9e37


# ---------------------------------------------------------------------------
# Profile accessors
# ---------------------------------------------------------------------------


def _measure_block(profile: dict[str, Any]) -> dict[str, Any]:
    cap = profile.get("capability") or {}
    measure = cap.get("measure")
    if not measure or not measure.get("items"):
        model = profile.get("model", "<unknown>")
        raise MeasureValidationError(
            f"profile for {model} declares no capability.measure.items; "
            "cannot validate measurement operations"
        )
    return measure


def measure_items(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the raw ``{canonical_name: spec}`` measurement-item map."""
    return dict(_measure_block(profile)["items"])


def _channels(profile: dict[str, Any]) -> int:
    return int((profile.get("capability") or {}).get("channels", 4))


# ---------------------------------------------------------------------------
# Normalization / validation
# ---------------------------------------------------------------------------


def normalize_item(profile: dict[str, Any], requested: str) -> tuple[str, dict[str, Any]]:
    """Resolve a user-supplied measurement-item name to its profile entry.

    Accepts the canonical name (``"FREQUENCY"``), the SCPI keyword in any
    spelling (``"FREQuency"`` / ``"FREQ"``), or any case variant — the
    match is case-insensitive and the RIGOL short form (capital letters of
    the SCPI keyword, e.g. ``FREQ`` for ``FREQuency``) is also accepted.

    Returns ``(canonical_name, spec)``. Raises
    :class:`MeasureValidationError` listing valid items if no match exists.
    """
    if not requested or not requested.strip():
        raise MeasureValidationError("measurement item must be non-empty")
    want = requested.strip().upper()
    items = measure_items(profile)

    for name, spec in items.items():
        if want == name.upper():
            return name, spec
    for name, spec in items.items():
        scpi = str(spec.get("scpi", name))
        if want == scpi.upper() or want == _short_form(scpi).upper():
            return name, spec

    valid = ", ".join(items)
    raise MeasureValidationError(
        f"measurement item {requested!r} is not supported by this model; "
        f"valid items: {valid}"
    )


def normalize_source(profile: dict[str, Any], source: Any) -> str:
    """Normalize a source to ``CHANnel<n>`` (the DS1000Z spelling used in
    ``:MEAS:ITEM? <item>,<src>``).

    A bare number (``2``), ``CHAN2`` or ``CHANnel2`` → ``CHANnel2``, with
    ``n`` bounded by ``capability.channels``. Anything else (digital
    sources, math, unknown tokens) is rejected — these are analog scopes.
    """
    s = str(source).strip().upper()
    m = re.fullmatch(r"(?:CHAN(?:NEL)?)?([0-9]+)", s)
    if not m:
        raise MeasureValidationError(
            f"source {source!r} invalid; expected a channel "
            f"(1..{_channels(profile)} or CHAN<n>)"
        )
    n = int(m.group(1))
    ch = _channels(profile)
    if not 1 <= n <= ch:
        raise MeasureValidationError(
            f"source channel {n} out of range (1..{ch})"
        )
    return f"CHANnel{n}"


def resolve_items(
    profile: dict[str, Any], requested: list[str]
) -> list[tuple[str, dict[str, Any]]]:
    """Validate and normalize a list of requested items, returning
    ``[(canonical_name, spec), …]`` in request order.

    Raises :class:`MeasureValidationError` on an empty list or any unknown
    item — before any SCPI is issued, mirroring the trigger engine.
    """
    if not requested:
        raise MeasureValidationError(
            "'items' is required and must list at least one measurement item"
        )
    return [normalize_item(profile, item) for item in requested]


def is_dual_source(spec: dict[str, Any]) -> bool:
    """True if the item requires a second source (delay / phase items)."""
    return bool(spec.get("dual_source"))


# ---------------------------------------------------------------------------
# Statistics validation / normalization
# ---------------------------------------------------------------------------


def _statistics_block(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the ``capability.measure.statistics`` sub-block."""
    measure = _measure_block(profile)
    stats = measure.get("statistics")
    if not stats:
        model = profile.get("model", "<unknown>")
        raise MeasureValidationError(
            f"profile for {model} declares no capability.measure.statistics; "
            "cannot validate measurement-statistics operations"
        )
    return stats


def validate_stat_type(profile: dict[str, Any], requested: str) -> str:
    """Normalize a stat-type string to its canonical profile form.

    Accepts the canonical name (``"CURRent"``), the RIGOL short form
    (``"CURR"``), or any case variant. Returns the canonical long-form
    keyword. Raises :class:`MeasureValidationError` on an unknown value.
    """
    if not requested or not requested.strip():
        raise MeasureValidationError("stat_type must be non-empty")
    want = requested.strip().upper()
    stats = _statistics_block(profile)
    for st in stats["stat_types"]:
        if want == st.upper() or want == _short_form(st).upper():
            return st
    valid = ", ".join(stats["stat_types"])
    raise MeasureValidationError(
        f"stat_type {requested!r} is not supported; valid: {valid}"
    )


def validate_stat_mode(profile: dict[str, Any], requested: str) -> str:
    """Normalize a statistics mode string to its canonical profile form.

    Accepts the canonical name (``"DIFFerence"``), the RIGOL short form
    (``"DIFF"``), or any case variant. Returns the canonical long-form
    keyword. Raises :class:`MeasureValidationError` on an unknown value.
    """
    if not requested or not requested.strip():
        raise MeasureValidationError("stat_mode must be non-empty")
    want = requested.strip().upper()
    stats = _statistics_block(profile)
    for mode in stats["modes"]:
        if want == mode.upper() or want == _short_form(mode).upper():
            return mode
    valid = ", ".join(stats["modes"])
    raise MeasureValidationError(
        f"statistics mode {requested!r} is not supported; valid: {valid}"
    )


def stat_types_list(profile: dict[str, Any]) -> list[str]:
    """Return the full list of canonical stat-type keywords from the profile."""
    return list(_statistics_block(profile)["stat_types"])


def units_for(items: list[tuple[str, dict[str, Any]]]) -> dict[str, str]:
    """Map ``{canonical_name: unit}`` for the resolved items (for the
    tool's ``units`` field). Items without a declared unit are omitted.
    """
    return {name: str(spec["unit"]) for name, spec in items if spec.get("unit")}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_measure_value(resp: str) -> float | None:
    """Coerce a raw ``:MEAS:ITEM?`` response into a float, or ``None`` when
    the instrument reports the item as un-measurable.

    The DS1000Z returns ``9.9E37`` (and some firmware ``9.91E37``) for an
    item it cannot currently measure; any value at or above
    :data:`UNMEASURABLE_SENTINEL` is treated as null. An empty / non-numeric
    response is also ``None`` (firmware quirk) rather than an exception, so
    a single bad item never fails the whole batch.
    """
    resp = (resp or "").strip()
    if resp == "":
        return None
    try:
        value = float(resp)
    except ValueError:
        return None
    if abs(value) >= UNMEASURABLE_SENTINEL:
        return None
    return value


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _short_form(v: str) -> str:
    """RIGOL keyword short form = its capital letters / digits
    (``FREQuency`` → ``FREQ``); matches the trigger helper's convention.
    """
    return "".join(c for c in v if c.isupper() or c.isdigit())
