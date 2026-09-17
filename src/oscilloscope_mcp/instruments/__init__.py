"""Instrument registry + dispatch.

Adding a new scope is three steps (see ``bench/README.md``):

1. Write ``profiles/<model>.yaml`` declaring capability + ``idn_match``.
2. Write a driver subclassing :class:`bench.instruments._base.Scope`.
3. Add a line to :data:`MODEL_REGISTRY` below.

After that, ``open_scope()`` resolves the right driver either from the
``SCOPE_MCP_MODEL`` environment variable or by parsing the
instrument's ``*IDN?`` against each profile's ``idn_match`` regex.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from oscilloscope_mcp.instruments._base import Scope
from oscilloscope_mcp.instruments.rigol_ds1000z import RigolDs1000z
from oscilloscope_mcp.instruments.zlg_zds1000 import ZlgZds1000
from oscilloscope_mcp.transport.scpi_lan import ScpiError, ScpiLan


# Source-of-truth dispatch table. Keys are stable model identifiers used
# in SCOPE_MCP_MODEL. Each entry is (driver_class, profile_filename).
MODEL_REGISTRY: dict[str, tuple[type[Scope], str]] = {
    "RIGOL_DS1104Z": (RigolDs1000z, "rigol_ds1104z.yaml"),
    "RIGOL_DS1054Z": (RigolDs1000z, "rigol_ds1054z.yaml"),
    "ZLG_ZDS1104": (ZlgZds1000, "zlg_zds1104.yaml"),
}


_HOST_ENV = "SCOPE_MCP_HOST"
_PORT_ENV = "SCOPE_MCP_PORT"
_MODEL_ENV = "SCOPE_MCP_MODEL"
_PROFILES_DIR = Path(__file__).parent / "profiles"


class ScopeDispatchError(RuntimeError):
    """Raised when ``open_scope()`` cannot resolve a driver."""


def resolve_host_port_from_env() -> tuple[str, int | None]:
    """Read ``SCOPE_MCP_HOST`` (required) and ``..._PORT`` (optional).

    Returns ``port=None`` when the env var is unset — the caller then
    resolves it from the selected profile's ``transport.default_port``
    (or probes the common SCPI ports when the model must be
    auto-detected first).
    """
    host = os.environ.get(_HOST_ENV)
    if not host:
        raise ScopeDispatchError(
            f"{_HOST_ENV} is not set; cannot connect to scope. "
            "Set it to the instrument's IP or hostname."
        )
    port_str = os.environ.get(_PORT_ENV)
    if port_str is None:
        return host, None
    try:
        port = int(port_str)
    except ValueError as e:
        raise ScopeDispatchError(
            f"{_PORT_ENV}={port_str!r} is not an integer"
        ) from e
    return host, port


def open_scope(
    host: str | None = None,
    port: int | None = None,
    model: str | None = None,
    timeout_s: float = 3.0,
) -> Scope:
    """Resolve a driver + profile for the target instrument and return
    a ready-to-use :class:`Scope`.

    Resolution order:

    - ``host`` / ``port`` default to the env vars when omitted. An unset
      port is resolved from the selected profile's
      ``transport.default_port``; when the model itself must be
      auto-detected (which needs a live connection first), the common
      SCPI ports (5555, 5025) are probed for one that answers
      ``*IDN?``.
    - ``model`` defaults to ``SCOPE_MCP_MODEL`` when omitted; if
      still missing, ``*IDN?`` is queried and matched against each
      profile's ``idn_match`` regex (exactly one match required).
    """
    port_given = port is not None or os.environ.get(_PORT_ENV) is not None
    if host is None or port is None:
        env_host, env_port = resolve_host_port_from_env()
        host = host or env_host
        port = port if port is not None else env_port

    model = model or os.environ.get(_MODEL_ENV) or None

    transport: ScpiLan | None = None
    if model is None:
        # Auto-detect needs a live connection before the profile (and
        # thus its default_port) is known.
        if port is None:
            port = _probe_scpi_port(host, timeout_s)
        transport = ScpiLan(host=host, port=port, timeout_s=timeout_s)
        model = _auto_detect_model(transport)

    if model not in MODEL_REGISTRY:
        raise ScopeDispatchError(
            f"model {model!r} not in MODEL_REGISTRY "
            f"(known: {sorted(MODEL_REGISTRY)}). Add a driver + profile "
            f"per bench/README.md to support it."
        )
    driver_cls, profile_filename = MODEL_REGISTRY[model]
    profile = _load_profile(profile_filename)

    if port is None:
        # No port configured anywhere: adopt the model's declared
        # default (RIGOL 5555, ZLG 5025); 5555 for legacy profiles
        # without a declaration.
        port = _profile_default_port(profile)
    if transport is None:
        transport = ScpiLan(host=host, port=port, timeout_s=timeout_s)
    elif not port_given and transport.port != port:
        transport.port = port
    return driver_cls(transport=transport, profile=profile)


# Common raw-TCP SCPI ports, probed in order when none is configured.
# RIGOL speaks on 5555; ZLG / Siglent / Keysight on 5025.
_PROBE_PORTS = (5555, 5025)


def _probe_scpi_port(host: str, timeout_s: float) -> int:
    """Find a TCP port that answers ``*IDN?`` when none was configured.

    Only used on the auto-detect path, which needs a live connection
    before the profile is known. First responsive port wins; the probe
    timeout is capped at 1 s so an unreachable host fails fast.
    """
    last: Exception | None = None
    for candidate in _PROBE_PORTS:
        probe = ScpiLan(
            host=host, port=candidate,
            timeout_s=min(timeout_s, 1.0), retries=0,
        )
        try:
            probe.query("*IDN?")
            return candidate
        except ScpiError as e:
            last = e
    raise ScopeDispatchError(
        f"no SCPI port answered *IDN? on {host} (tried "
        f"{list(_PROBE_PORTS)}); set {_PORT_ENV} explicitly. "
        f"Last error: {last}"
    )


def _profile_default_port(profile: dict[str, Any]) -> int:
    """The profile's declared SCPI port, or 5555 (RIGOL convention) when
    the profile declares none."""
    declared = (profile.get("transport") or {}).get("default_port")
    try:
        return int(declared)
    except (TypeError, ValueError):
        return 5555


def _auto_detect_model(transport: ScpiLan) -> str:
    """Query *IDN? and match against each profile's idn_match regex.

    Exactly one match is required — zero hits or multiple hits raise
    :class:`ScopeDispatchError` so the caller sets
    ``SCOPE_MCP_MODEL`` explicitly.
    """
    idn = transport.query("*IDN?")
    hits: list[str] = []
    for model, (_, profile_filename) in MODEL_REGISTRY.items():
        profile = _load_profile(profile_filename)
        pattern = profile.get("idn_match")
        if pattern and re.search(pattern, idn):
            hits.append(model)
    if not hits:
        raise ScopeDispatchError(
            f"auto-detect failed: no profile.idn_match matched IDN={idn!r}. "
            f"Set {_MODEL_ENV} explicitly or add a profile."
        )
    if len(hits) > 1:
        raise ScopeDispatchError(
            f"auto-detect ambiguous: IDN={idn!r} matched {hits}. "
            f"Set {_MODEL_ENV} explicitly to disambiguate."
        )
    return hits[0]


def _load_profile(filename: str) -> dict[str, Any]:
    """Load a profile YAML by filename (relative to profiles/ dir).

    A profile may declare ``includes: [<fragment>.yaml, …]`` to pull in
    shared capability fragments (e.g. a whole instrument family's trigger
    schema lives in one ``_<family>.yaml`` so per-model profiles don't
    duplicate it). Fragments are deep-merged in listed order, then the
    profile's own keys are merged on top (the profile always wins on
    conflict, so a model can override a family default).
    """
    parsed = _read_yaml_mapping(filename)
    includes = parsed.pop("includes", None)
    if not includes:
        return parsed
    if not isinstance(includes, list):
        raise ScopeDispatchError(
            f"profile {filename} 'includes' must be a list, got "
            f"{type(includes).__name__}"
        )
    merged: dict[str, Any] = {}
    for frag_name in includes:
        _deep_merge(merged, _read_yaml_mapping(str(frag_name)))
    _deep_merge(merged, parsed)
    return merged


def _read_yaml_mapping(filename: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as e:
        raise ScopeDispatchError(
            "PyYAML is required to load instrument profiles; "
            "install via `pip install pyyaml` or run inside devcontainer."
        ) from e
    path = _PROFILES_DIR / filename
    if not path.is_file():
        raise ScopeDispatchError(f"profile not found: {path}")
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ScopeDispatchError(
            f"profile {path} must be a YAML mapping, got {type(parsed).__name__}"
        )
    return parsed


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Recursively merge ``override`` into ``base`` in place. Nested dicts
    merge key-by-key; any non-dict value (including lists) from
    ``override`` replaces the value in ``base``.
    """
    for key, val in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(val, dict)
        ):
            _deep_merge(base[key], val)
        else:
            base[key] = val
