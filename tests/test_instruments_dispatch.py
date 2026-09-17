"""Unit tests for :mod:`bench.instruments` dispatch.

Covers env-var resolution, MODEL_REGISTRY lookup, and *IDN? auto-detect
without touching real hardware (the dispatch path is patched).
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp import instruments
from oscilloscope_mcp.instruments import (
    MODEL_REGISTRY,
    ScopeDispatchError,
    open_scope,
    resolve_host_port_from_env,
)
from oscilloscope_mcp.transport.scpi_lan import ScpiError


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "SCOPE_MCP_HOST",
        "SCOPE_MCP_PORT",
        "SCOPE_MCP_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_resolve_host_port_requires_host(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ScopeDispatchError, match="SCOPE_MCP_HOST"):
        resolve_host_port_from_env()


def test_resolve_host_port_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset SCOPE_MCP_PORT now resolves to None — the caller falls
    back to the profile's transport.default_port (or probes)."""
    monkeypatch.setenv("SCOPE_MCP_HOST", "10.0.0.5")
    host, port = resolve_host_port_from_env()
    assert host == "10.0.0.5"
    assert port is None


def test_resolve_host_port_explicit_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPE_MCP_HOST", "10.0.0.5")
    monkeypatch.setenv("SCOPE_MCP_PORT", "5025")
    _, port = resolve_host_port_from_env()
    assert port == 5025


def test_resolve_host_port_rejects_non_integer_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPE_MCP_HOST", "10.0.0.5")
    monkeypatch.setenv("SCOPE_MCP_PORT", "not-a-number")
    with pytest.raises(ScopeDispatchError, match="not an integer"):
        resolve_host_port_from_env()


def test_registry_includes_known_rigol_models() -> None:
    assert "RIGOL_DS1104Z" in MODEL_REGISTRY
    assert "RIGOL_DS1054Z" in MODEL_REGISTRY


def test_open_scope_unknown_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPE_MCP_HOST", "127.0.0.1")
    with pytest.raises(ScopeDispatchError, match="not in MODEL_REGISTRY"):
        open_scope(model="UNKNOWN_BRAND_99")


def test_open_scope_uses_profile_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no port configured and the model known, the transport lands
    on the model profile's transport.default_port (RIGOL 5555, ZLG 5025)
    — no probing needed because the model is resolved up front."""
    monkeypatch.setenv("SCOPE_MCP_HOST", "127.0.0.1")

    class _StubTransport:
        def __init__(self, **kw: object) -> None:
            self.__dict__.update(kw)

        def query(self, cmd: str) -> str:
            return "stub"

    monkeypatch.setattr(instruments, "ScpiLan", lambda **kw: _StubTransport(**kw))
    rigol = open_scope(model="RIGOL_DS1104Z")
    assert rigol.transport.port == 5555
    zlg = open_scope(model="ZLG_ZDS1104")
    assert zlg.transport.port == 5025


def test_open_scope_explicit_port_beats_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPE_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("SCOPE_MCP_PORT", "5556")

    class _StubTransport:
        def __init__(self, **kw: object) -> None:
            self.__dict__.update(kw)

        def query(self, cmd: str) -> str:
            return "stub"

    monkeypatch.setattr(instruments, "ScpiLan", lambda **kw: _StubTransport(**kw))
    scope = open_scope(model="ZLG_ZDS1104")
    assert scope.transport.port == 5556


def test_open_scope_probes_ports_for_auto_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-detect needs a live connection before the profile is known;
    with no port configured, the common SCPI ports are probed and the
    first one that answers *IDN? wins."""
    monkeypatch.setenv("SCOPE_MCP_HOST", "127.0.0.1")

    class _StubTransport:
        def __init__(self, **kw: object) -> None:
            self.__dict__.update(kw)

        def query(self, cmd: str) -> str:
            assert cmd == "*IDN?"
            if self.port == 5555:
                raise ScpiError("connection refused (probe)")
            return "ZHIYUANELECT,ZDS1104,SN0001,V1.0,1.0.0"

    monkeypatch.setattr(instruments, "ScpiLan", lambda **kw: _StubTransport(**kw))
    scope = open_scope()
    assert scope.profile["model"] == "ZLG_ZDS1104"
    assert scope.transport.port == 5025


def test_auto_detect_matches_idn_to_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the transport query so *IDN? returns a string that the
    DS1104Z profile's idn_match regex captures.
    """
    monkeypatch.setenv("SCOPE_MCP_HOST", "127.0.0.1")

    class _StubTransport:
        host = "127.0.0.1"
        port = 5555
        timeout_s = 1.0

        def query(self, cmd: str) -> str:
            return "RIGOL TECHNOLOGIES,DS1104Z,DS1ZA191003238,00.04.04.SP1"

    monkeypatch.setattr(instruments, "ScpiLan", lambda **kw: _StubTransport())
    scope = open_scope()
    assert scope.profile["model"] == "RIGOL_DS1104Z"


def test_auto_detect_no_match_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPE_MCP_HOST", "127.0.0.1")

    class _StubTransport:
        host = "127.0.0.1"
        port = 5555
        timeout_s = 1.0

        def query(self, cmd: str) -> str:
            return "SOMECORP,UNKNOWN_MODEL,SN,FW"

    monkeypatch.setattr(instruments, "ScpiLan", lambda **kw: _StubTransport())
    with pytest.raises(ScopeDispatchError, match="no profile.idn_match matched"):
        open_scope()


def test_profile_yaml_loads_for_each_registered_model() -> None:
    """Sanity-check that every MODEL_REGISTRY entry has a loadable
    profile with the required idn_match field. Catches typos in either
    side of the registry mapping.
    """
    for model, (_, filename) in MODEL_REGISTRY.items():
        profile = instruments._load_profile(filename)
        assert profile.get("model") == model
        assert "idn_match" in profile, f"{filename} missing idn_match"
        assert "capability" in profile, f"{filename} missing capability"


# Per-family trigger inventories, each from its vendor's programming
# guide. RIGOL: MSO1000Z/DS1000Z §2 (6 standard + 9 option-licensed).
# ZLG: ZDS1000 manual §16 (11 types, none option-licensed).
# (keyword, query, option).
_FAMILY_TRIGGER_TYPES = {
    "DS1000Z": {
        ("EDGE", "EDGE", False),
        ("PULSe", "PULS", False),
        ("SLOPe", "SLOP", False),
        ("VIDeo", "VID", False),
        ("PATTern", "PATT", False),
        ("DURation", "DUR", False),
        ("TIMeout", "TIM", True),
        ("RUNT", "RUNT", True),
        ("WIND", "WIND", True),
        ("DELay", "DEL", True),
        ("SHOLd", "SHOL", True),
        ("NEDG", "NEDG", True),
        ("RS232", "RS232", True),
        ("IIC", "IIC", True),
        ("SPI", "SPI", True),
    },
    "ZDS1000": {
        ("EDGE", "EDGE", False),
        ("PULSe", "PULS", False),
        ("SLOPe", "SLOP", False),
        ("VIDeo", "VID", False),
        ("RUNT", "RUNT", False),
        ("PRUNt", "PRUN", False),
        ("PATTern", "PATT", False),
        ("NEDGe", "NEDG", False),
        ("DELay", "DEL", False),
        ("TIMeout", "TIM", False),
        ("SHOLd", "SHOL", False),
    },
}

# Per-family sweep/status/run-control expectations. ZDS1000 quirks:
# SINGLE is a run action (not a sweep mode), status comes from
# :GLOBal:RUN:STATe?, and FORCE has no SCPI equivalent yet.
_FAMILY_TRIGGER_META = {
    "DS1000Z": {
        "sweep_modes": ["AUTO", "NORMAL", "SINGLE"],
        "status_values": {"TD", "WAIT", "RUN", "AUTO", "STOP"},
        "run_control": {"RUN", "STOP", "SINGLE", "FORCE"},
    },
    "ZDS1000": {
        "sweep_modes": ["AUTO", "NORMAL"],
        "status_values": {"Run", "Single", "Stop"},
        "run_control": {"RUN", "STOP", "SINGLE"},
    },
}


@pytest.mark.parametrize("model", sorted(MODEL_REGISTRY))
def test_profile_declares_complete_family_trigger_inventory(model: str) -> None:
    """Every profile must declare the full :TRIGger:MODE type set its
    family's instruction set accepts, so scope_trigger can validate
    against it. Guards against silently dropping a type.
    """
    _, filename = MODEL_REGISTRY[model]
    profile = instruments._load_profile(filename)
    family = profile.get("family")
    assert family in _FAMILY_TRIGGER_TYPES, (
        f"{filename} declares unknown family {family!r}; add its trigger "
        "inventory to _FAMILY_TRIGGER_TYPES"
    )
    trig = profile["capability"]["trigger"]

    declared = {
        (t["keyword"], t["query"], bool(t["option"])) for t in trig["types"]
    }
    assert declared == _FAMILY_TRIGGER_TYPES[family], (
        f"{filename} trigger inventory drifted from the programming guide"
    )
    # Query-return forms must be unique — they are how :TRIG:MODE? results
    # are mapped back to a known type.
    queries = [t["query"] for t in trig["types"]]
    assert len(queries) == len(set(queries)), "duplicate :TRIG:MODE? form"

    meta = _FAMILY_TRIGGER_META[family]
    assert trig["sweep_modes"] == meta["sweep_modes"]
    assert set(trig["status_values"]) == meta["status_values"]

    # Every type must declare a non-empty params schema, and each param
    # must carry a SCPI node + a known kind so the engine can act on it.
    known_kinds = {"source", "enum", "real", "int", "bool", "pattern", "chan_level"}
    for t in trig["types"]:
        params = t.get("params")
        assert params, f"{filename}: {t['keyword']} declares no params"
        for pname, spec in params.items():
            assert "scpi" in spec, f"{t['keyword']}.{pname} missing scpi node"
            assert spec.get("kind") in known_kinds, (
                f"{t['keyword']}.{pname} has unknown kind {spec.get('kind')!r}"
            )
            if spec["kind"] == "enum":
                assert spec.get("values"), f"{t['keyword']}.{pname} enum needs values"

    # Run/arm control actions must be declared for the run-control path.
    assert set(trig.get("run_control", {})) == meta["run_control"], (
        f"{filename} run_control actions drifted"
    )


def test_includes_deep_merge_shares_family_trigger_schema() -> None:
    """Both DS1000Z profiles pull the trigger schema from the shared
    family fragment via `includes`, and the merged result keeps each
    model's own capability fields (analog BW differs per model).
    """
    p1104 = instruments._load_profile("rigol_ds1104z.yaml")
    p1054 = instruments._load_profile("rigol_ds1054z.yaml")
    # 'includes' is consumed by the loader, not surfaced in the result.
    assert "includes" not in p1104 and "includes" not in p1054
    # Shared trigger schema is byte-for-byte identical after merge.
    assert p1104["capability"]["trigger"] == p1054["capability"]["trigger"]
    # Model-specific fields survive the merge and differ.
    assert p1104["capability"]["analog_bw_hz"] != p1054["capability"]["analog_bw_hz"]
