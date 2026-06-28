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
    monkeypatch.setenv("SCOPE_MCP_HOST", "10.0.0.5")
    host, port = resolve_host_port_from_env()
    assert host == "10.0.0.5"
    assert port == 5555


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


# The complete set of trigger types the DS1000Z instruction set accepts,
# per RIGOL MSO1000Z/DS1000Z Programming Guide (§2, :TRIGger:MODE). The
# 6 standard types are always available; the 9 marked option-licensed
# require the advanced-trigger bundle. (keyword, query, option).
_DS1000Z_TRIGGER_TYPES = {
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
}


@pytest.mark.parametrize("model", sorted(MODEL_REGISTRY))
def test_profile_declares_complete_ds1000z_trigger_inventory(model: str) -> None:
    """Every DS1000Z profile must declare the full :TRIG:MODE type set so
    a future scope_trigger tool can validate against it. Guards against
    silently dropping a type (e.g. forgetting the option-licensed ones).
    """
    _, filename = MODEL_REGISTRY[model]
    profile = instruments._load_profile(filename)
    trig = profile["capability"]["trigger"]

    declared = {
        (t["keyword"], t["query"], bool(t["option"])) for t in trig["types"]
    }
    assert declared == _DS1000Z_TRIGGER_TYPES, (
        f"{filename} trigger inventory drifted from the programming guide"
    )
    # Query-return forms must be unique — they are how :TRIG:MODE? results
    # are mapped back to a known type.
    queries = [t["query"] for t in trig["types"]]
    assert len(queries) == len(set(queries)), "duplicate :TRIG:MODE? form"
    assert trig["sweep_modes"] == ["AUTO", "NORMAL", "SINGLE"]
    assert set(trig["status_values"]) == {"TD", "WAIT", "RUN", "AUTO", "STOP"}

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
    assert set(trig.get("run_control", {})) == {"RUN", "STOP", "SINGLE", "FORCE"}, (
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
