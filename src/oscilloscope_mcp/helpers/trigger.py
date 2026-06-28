"""Trigger profile interpretation — validation, normalization, caveats.

The capability profile declares the *complete* set of trigger types the
instrument's instruction set accepts (``capability.trigger`` block, see
``instruments/profiles/<model>.yaml``). This module is the single place
that reads that block so the driver and the ``scope_trigger`` MCP tool
never hard-code the type list.

Three concerns live here, all pure functions over the profile dict:

1. **Validation** — reject a trigger type the model does not list, with
   an error that enumerates the valid options (so the agent can recover
   without reading the YAML).
2. **Normalization** — the user (or the agent) may pass ``"edge"`` or
   ``"PULS"`` or ``"PULSe"``; map any of those to the canonical profile
   entry. Map a raw ``:TRIG:MODE?`` response (short form) back too.
3. **Caveats** — option-licensed types are accepted by the instruction
   set but silently ignored on a unit without the license; surface that.
"""

from __future__ import annotations

import re
from typing import Any


class TriggerValidationError(ValueError):
    """Raised when a requested trigger type / sweep / coupling is not
    declared in the model profile. Subclass of :class:`ValueError` so the
    MCP layer surfaces it as a clean tool error.
    """


# ---------------------------------------------------------------------------
# Profile accessors
# ---------------------------------------------------------------------------


def _trigger_block(profile: dict[str, Any]) -> dict[str, Any]:
    cap = profile.get("capability") or {}
    trig = cap.get("trigger")
    if not trig or not trig.get("types"):
        model = profile.get("model", "<unknown>")
        raise TriggerValidationError(
            f"profile for {model} declares no capability.trigger.types; "
            "cannot validate trigger operations"
        )
    return trig


def trigger_types(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the raw list of trigger-type entries from the profile."""
    return list(_trigger_block(profile)["types"])


# ---------------------------------------------------------------------------
# Normalization / validation
# ---------------------------------------------------------------------------


def normalize_trigger_keyword(
    profile: dict[str, Any], requested: str
) -> dict[str, Any]:
    """Resolve a user-supplied trigger keyword to its profile entry.

    Accepts the long form (``"PULSe"``), the SCPI short / query form
    (``"PULS"``), or any case variant — match is case-insensitive and a
    prefix of the long form is accepted only when it is the exact short
    (query) form, to avoid ambiguous abbreviations.

    Returns the matched ``{keyword, query, subsystem, option}`` dict.
    Raises :class:`TriggerValidationError` listing valid keywords if no
    unambiguous match exists.
    """
    if not requested or not requested.strip():
        raise TriggerValidationError("trigger 'mode' must be non-empty")
    want = requested.strip().upper()
    types = trigger_types(profile)

    for entry in types:
        if want == str(entry["keyword"]).upper():
            return entry
    for entry in types:
        if want == str(entry["query"]).upper():
            return entry

    valid = ", ".join(str(e["keyword"]) for e in types)
    raise TriggerValidationError(
        f"trigger mode {requested!r} is not supported by this model; "
        f"valid types: {valid}"
    )


def keyword_from_query(profile: dict[str, Any], query_response: str) -> str:
    """Map a raw ``:TRIG:MODE?`` response (short form, e.g. ``"PULS"``)
    back to the canonical long-form keyword (``"PULSe"``).

    Returns the raw response unchanged if it matches nothing in the
    profile (so the caller still sees *something* rather than crashing on
    a firmware quirk).
    """
    resp = query_response.strip().upper()
    for entry in trigger_types(profile):
        if resp == str(entry["query"]).upper() or resp == str(entry["keyword"]).upper():
            return str(entry["keyword"])
    return query_response.strip()


def validate_sweep(profile: dict[str, Any], sweep: str) -> str:
    """Validate a sweep mode against the profile, return the canonical
    upper-case form (``AUTO`` / ``NORMAL`` / ``SINGLE``).
    """
    allowed = [str(s).upper() for s in _trigger_block(profile).get("sweep_modes", [])]
    # Accept the SCPI long form NORMal → NORMAL, SINGle → SINGLE.
    want = sweep.strip().upper()
    aliases = {"NORM": "NORMAL", "SING": "SINGLE"}
    want = aliases.get(want, want)
    if want not in allowed:
        raise TriggerValidationError(
            f"sweep {sweep!r} invalid; valid: {', '.join(allowed)}"
        )
    return want


def validate_coupling(profile: dict[str, Any], coupling: str) -> str:
    """Validate a trigger-coupling mode against the profile, return the
    canonical form as the profile spells it (e.g. ``LFReject``).
    """
    allowed = list(_trigger_block(profile).get("coupling_modes", []))
    want = coupling.strip().upper()
    for entry in allowed:
        if want == str(entry).upper():
            return str(entry)
    raise TriggerValidationError(
        f"coupling {coupling!r} invalid; valid: {', '.join(str(c) for c in allowed)}"
    )


# ---------------------------------------------------------------------------
# Caveats
# ---------------------------------------------------------------------------


def run_actions(profile: dict[str, Any]) -> dict[str, str]:
    """Return the model's ``{action: scpi}`` run/arm control map (RUN /
    STOP / SINGLE / FORCE → root commands)."""
    return dict(_trigger_block(profile).get("run_control", {}))


def resolve_run_action(profile: dict[str, Any], action: str) -> tuple[str, str]:
    """Resolve a run-control action (case-insensitive) to its
    ``(canonical_name, scpi)``. Raises :class:`TriggerValidationError`
    listing valid actions if unknown.
    """
    actions = run_actions(profile)
    if not actions:
        raise TriggerValidationError(
            "this model declares no trigger run_control actions"
        )
    want = (action or "").strip().upper()
    for name, scpi in actions.items():
        if want == str(name).upper():
            return str(name), str(scpi)
    raise TriggerValidationError(
        f"run action {action!r} invalid; valid: {', '.join(actions)}"
    )


def caveats_for_trigger_type(entry: dict[str, Any]) -> list[str]:
    """Caveat emitted when an option-licensed trigger type is selected.

    The instruction set accepts these keywords on every DS1000Z, but a
    unit without the advanced-trigger option license silently ignores the
    selection (the mode does not actually change). The agent must not
    assume the trigger took effect — it should read back the mode.
    """
    if entry.get("option"):
        return [
            f"trigger type {entry['keyword']!r} is an option-licensed mode; "
            "on a unit without the advanced-trigger license the instrument "
            "silently ignores it. Read back :TRIG:MODE? to confirm it applied."
        ]
    return []


# ---------------------------------------------------------------------------
# Per-type parameter engine (data-driven over profile.trigger.types[].params)
# ---------------------------------------------------------------------------
#
# Each trigger type declares a `params` map in the profile:
#   canonical_name: {scpi, kind, values?, tokens?, named?, min?, max?, unit?}
# The functions below validate a caller-supplied {name: value} dict against
# that schema and produce SCPI writes / read queries, so the driver and the
# MCP tool never hard-code any of the ~95 mode-specific parameters.


def build_param_writes(
    profile: dict[str, Any],
    mode_entry: dict[str, Any],
    params: dict[str, Any],
) -> list[tuple[str, str]]:
    """Validate ``params`` against ``mode_entry``'s schema and return a list
    of ``(scpi_node, formatted_value)`` writes (e.g.
    ``(":TRIGger:NEDGe:EDGE", "2")``). Raises :class:`TriggerValidationError`
    on an unknown parameter or an out-of-range / wrong-type value, before
    any write is performed.
    """
    schema = mode_entry.get("params") or {}
    subsystem = mode_entry["subsystem"]
    writes: list[tuple[str, str]] = []
    for name, value in params.items():
        if name not in schema:
            valid = ", ".join(sorted(schema)) or "(this mode takes no parameters)"
            raise TriggerValidationError(
                f"parameter {name!r} is not valid for trigger mode "
                f"{mode_entry['keyword']!r}; valid params: {valid}"
            )
        spec = schema[name]
        formatted = _format_param(profile, name, spec, value)
        writes.append((f"{subsystem}:{spec['scpi']}", formatted))
    return writes


# Kinds that are write-only for the generic reader: a bare ``?`` query
# either needs an argument (chan_level → ``:LEVel? <chan>``) or hangs on
# some firmware (per-channel ``pattern`` like :DURATion:TYPe?). These are
# still fully settable; they are just skipped on read-back.
_UNREADABLE_KINDS = {"pattern", "chan_level"}


def param_read_queries(
    mode_entry: dict[str, Any],
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Return ``{canonical_name: (query_scpi, spec)}`` for every *readable*
    parameter of the mode, so the driver can read the mode-specific state
    back. Params of an unreadable kind, or explicitly ``readable: false``,
    are omitted (they need a query argument or are not plain-queryable).
    """
    schema = mode_entry.get("params") or {}
    subsystem = mode_entry["subsystem"]
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, spec in schema.items():
        if spec.get("readable") is False or spec.get("kind") in _UNREADABLE_KINDS:
            continue
        out[name] = (f"{subsystem}:{spec['scpi']}?", spec)
    return out


def parse_param_value(spec: dict[str, Any], resp: str) -> Any:
    """Coerce a raw SCPI query response into a typed value per the param
    kind (real→float, int→int; everything else stays a trimmed string).
    Returns None for an empty response.
    """
    resp = (resp or "").strip()
    if resp == "":
        return None
    kind = spec.get("kind")
    if kind == "real":
        try:
            return float(resp)
        except ValueError:
            return resp
    if kind == "int":
        try:
            return int(float(resp))
        except ValueError:
            return resp
    return resp


def describe_mode_params(mode_entry: dict[str, Any]) -> dict[str, Any]:
    """Compact, agent-readable description of a mode's accepted parameters,
    used in read responses and validation errors so a caller can discover
    what to send without reading the YAML.
    """
    out: dict[str, Any] = {}
    subsystem = mode_entry["subsystem"]
    for name, spec in (mode_entry.get("params") or {}).items():
        d: dict[str, Any] = {"kind": spec["kind"], "scpi": f"{subsystem}:{spec['scpi']}"}
        for k in ("values", "tokens", "named", "min", "max", "unit"):
            if k in spec:
                d[k] = spec[k]
        out[name] = d
    return out


# --- per-kind formatters --------------------------------------------------


def _format_param(
    profile: dict[str, Any], name: str, spec: dict[str, Any], value: Any
) -> str:
    kind = spec.get("kind")
    if kind == "source":
        return _format_source(profile, spec, value)
    if kind == "enum":
        return _format_enum(name, spec, value)
    if kind == "real":
        return _format_real(name, spec, value)
    if kind == "int":
        return _format_int(name, spec, value)
    if kind == "bool":
        return _format_bool(name, value)
    if kind == "pattern":
        return _format_pattern(name, spec, value)
    if kind == "chan_level":
        return _format_chan_level(profile, name, value)
    raise TriggerValidationError(f"parameter {name!r} has unknown kind {kind!r}")


def _channels(profile: dict[str, Any]) -> int:
    return int((profile.get("capability") or {}).get("channels", 4))


def _format_source(
    profile: dict[str, Any], spec: dict[str, Any], value: Any
) -> str:
    """A bare number or CHAN<n> → ``CHAN<n>`` (n bounded by channel count);
    a named source (e.g. AC) must be listed in the spec's ``named``. Digital
    (D0–D15) and unknown sources are rejected — these are analog scopes.
    """
    s = str(value).strip().upper()
    m = re.fullmatch(r"(?:CHAN(?:NEL)?)?([0-9]+)", s)
    if m:
        n = int(m.group(1))
        ch = _channels(profile)
        if not 1 <= n <= ch:
            raise TriggerValidationError(
                f"source channel {n} out of range (1..{ch})"
            )
        return f"CHAN{n}"
    named = [str(x).upper() for x in spec.get("named", [])]
    if s in named:
        return s
    allowed = ", ".join([f"CHAN1..CHAN{_channels(profile)}"] + named)
    raise TriggerValidationError(f"source {value!r} invalid; allowed: {allowed}")


def _short_form(v: str) -> str:
    """RIGOL keyword short form = its capital letters / digits (POSitive→POS)."""
    return "".join(c for c in v if c.isupper() or c.isdigit())


# Friendly aliases accepted for slope-like enums (mapped to a RIGOL short
# form, then matched against the spec's values like any other input).
_ENUM_ALIASES = {
    "RISE": "POS", "RISING": "POS", "UP": "POS",
    "FALL": "NEG", "FALLING": "NEG", "DOWN": "NEG",
    "BOTH": "RFAL", "EITHER": "RFAL",
}


def _format_enum(name: str, spec: dict[str, Any], value: Any) -> str:
    want = str(value).strip().upper()
    candidates = {want}
    if want in _ENUM_ALIASES:
        candidates.add(_ENUM_ALIASES[want])
    for v in spec["values"]:
        sv = str(v)
        if sv.upper() in candidates or _short_form(sv).upper() in candidates:
            return sv  # send the long form; the instrument accepts it
    valid = ", ".join(str(v) for v in spec["values"])
    raise TriggerValidationError(f"{name}={value!r} invalid; valid: {valid}")


def _format_real(name: str, spec: dict[str, Any], value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise TriggerValidationError(f"{name}={value!r} must be a number")
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and x < float(lo):
        raise TriggerValidationError(f"{name}={value} below minimum {lo}")
    if hi is not None and x > float(hi):
        raise TriggerValidationError(f"{name}={value} above maximum {hi}")
    return f"{x:g}"


def _format_int(name: str, spec: dict[str, Any], value: Any) -> str:
    try:
        fx = float(value)
    except (TypeError, ValueError):
        raise TriggerValidationError(f"{name}={value!r} must be an integer")
    if fx != int(fx):
        raise TriggerValidationError(f"{name}={value!r} must be a whole number")
    n = int(fx)
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and n < int(lo):
        raise TriggerValidationError(f"{name}={n} below minimum {lo}")
    if hi is not None and n > int(hi):
        raise TriggerValidationError(f"{name}={n} above maximum {hi}")
    return str(n)


def _format_bool(name: str, value: Any) -> str:
    truthy = {"1", "ON", "TRUE", "YES"}
    falsy = {"0", "OFF", "FALSE", "NO"}
    s = str(value).strip().upper()
    if s in truthy or value is True:
        return "ON"
    if s in falsy or value is False:
        return "OFF"
    raise TriggerValidationError(f"{name}={value!r} must be a boolean (ON/OFF)")


def _format_pattern(name: str, spec: dict[str, Any], value: Any) -> str:
    tokens = {str(t).upper() for t in spec["tokens"]}
    if isinstance(value, str):
        items = [x.strip() for x in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        raise TriggerValidationError(
            f"{name} must be a comma-separated string or list of symbols"
        )
    out: list[str] = []
    for it in items:
        u = it.upper()
        if u not in tokens:
            valid = ", ".join(str(t) for t in spec["tokens"])
            raise TriggerValidationError(
                f"{name} symbol {it!r} invalid; valid: {valid}"
            )
        out.append(u)
    return ",".join(out)


def _format_chan_level(profile: dict[str, Any], name: str, value: Any) -> str:
    """``<CHANn>,<level>`` for per-channel level commands (PATTern:LEVel)."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        chan, lvl = value
    elif isinstance(value, str) and "," in value:
        chan, lvl = value.split(",", 1)
    else:
        raise TriggerValidationError(
            f"{name} must be '<CHANn>,<level>' or [channel, level]"
        )
    chan_fmt = _format_source(profile, {}, chan)
    try:
        lvl_f = float(lvl)
    except (TypeError, ValueError):
        raise TriggerValidationError(f"{name} level {lvl!r} must be a number")
    return f"{chan_fmt},{lvl_f:g}"
