"""Unit tests for skills/scope-bench/scripts/scope_cli.py — no hardware.

Mirrors the driver-test pattern: a recording fake transport serves a
canned response table, so run_doctor / run_dump exercise the real
RIGOL and ZDS drivers end-to-end (validation, parsing, file writes)
without a live instrument. main()'s wiring (argparse → open_scope →
sub-command) is covered via monkeypatched open_scope.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parents[1] / (
    "skills/scope-bench/scripts/scope_cli.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("scope_cli", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scope_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


scope_cli = _load_script()

from oscilloscope_mcp.instruments import _load_profile  # noqa: E402
from oscilloscope_mcp.instruments.rigol_ds1000z import RigolDs1000z  # noqa: E402
from oscilloscope_mcp.instruments.zlg_zds1000 import (  # noqa: E402
    ZlgZds1000,
    _WFM_HEAD,
    _WFM_ITEM,
)


# ---------------------------------------------------------------------------
# Fake transports (same pattern as tests/test_rigol_ds1000z_driver.py)
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """In-memory SCPI transport: records writes, serves a query table."""

    def __init__(
        self,
        response_map: dict[str, str] | None = None,
        binary_response: bytes | None = None,
        lp_binary_response: bytes | None = None,
    ) -> None:
        self.writes: list[str] = []
        self.queries: list[str] = []
        self._responses = response_map or {}
        self._binary = binary_response or b""
        self._lp_binary = lp_binary_response or b""

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)

    def write_many(self, cmds: list[str]) -> None:
        self.writes.extend(cmds)

    def query(self, cmd: str) -> str:
        self.queries.append(cmd)
        if cmd in self._responses:
            return self._responses[cmd]
        if cmd == "*IDN?":
            return "RIGOL TECHNOLOGIES,DS1104Z,SN,FW"
        if cmd == ":TRIG:STAT?":
            return "STOP"
        if cmd == ":TRIG:POS?":
            return "0"
        if cmd == ":TIM:MAIN:SCAL?":
            return "1.000000e-06"
        if cmd.endswith(":DISP?"):
            return "1"
        if cmd.endswith((":SCAL?", ":OFFS?", ":OFFSet?")):
            return "0.000000e+00"
        return ""

    def query_binary(self, cmd: str) -> bytes:
        self.queries.append(cmd)
        return self._binary

    def query_length_prefixed(self, cmd: str) -> bytes:
        self.queries.append(cmd)
        return self._lp_binary


# Eight BYTE samples: 0,0,0,3,3,3,0,0 volts (yinc=1, yorigin=yref=0).
_WAV_PREAMBLE = "0,0,8,1,1.000000e-06,0.000000e+00,0,1.000000e+00,0,0"
_WAV_BYTES = bytes([0, 0, 0, 3, 3, 3, 0, 0])

_RIGOL_FULL = {
    ":WAV:PREamble?": _WAV_PREAMBLE,
    ":ACQ:TYPE?": "NORM",
    ":ACQ:AVER?": "2",
    ":ACQ:MDEP?": "AUTO",
    ":ACQ:SRAT?": "1.000000e+09",
    ":TIM:MAIN:OFFS?": "0.000000e+00",
    ":TIM:MODE?": "MAIN",
    ":TRIG:MODE?": "EDGE",
    ":TRIG:SWE?": "AUTO",
    ":TRIG:COUP?": "DC",
    ":TRIG:HOLD?": "8.000000e-09",
    ":TRIGger:EDGe:SOURce?": "CHAN1",
    ":TRIGger:EDGe:SLOPe?": "POS",
    ":TRIGger:EDGe:LEVel?": "1.500000e+00",
}

_RIGOL_PROFILE = _load_profile("rigol_ds1104z.yaml")
_ZDS_PROFILE = _load_profile("zlg_zds1104.yaml")


def _make_rigol(
    **kw: Any,
) -> tuple[RigolDs1000z, _RecordingTransport]:
    kw.setdefault("response_map", dict(_RIGOL_FULL))
    t = _RecordingTransport(**kw)
    drv = RigolDs1000z(transport=t, profile=_RIGOL_PROFILE)
    drv.run_status_settle_s = 0
    return drv, t


def _wfm_payload(channel: int = 1, samples: bytes = b"\x80\x96\xa0",
                 sample_rate: float = 1.0e9, vert_div: float = 1.0e-3) -> bytes:
    """Synthetic ZDS WFM stream: Head + Item + Data (same layout the
    driver parses)."""
    data_off = _WFM_HEAD.size + _WFM_ITEM.size
    head = _WFM_HEAD.pack(
        b"WFM\x00", b"ZDS1104", b"V1.0", b"V1.00", 0,
        1.0e-3, 0.0, 0.0, 0, 0, sample_rate, 0, 0,
        b"EDGE", 1, *([0] * 17),
    )
    item = _WFM_ITEM.pack(
        channel, 0, 0, 0, 0, 0, 10.0, vert_div, 0.0,
        len(samples), data_off, *([0] * 16),
    )
    return head + item + samples


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_rigol_snapshot(tmp_path: Path) -> None:
    drv, t = _make_rigol()
    out = scope_cli.run_doctor(
        drv, host="10.0.0.1", save=False, data_dir=tmp_path
    )
    assert out["model"] == "RIGOL_DS1104Z"
    assert out["host"] == "10.0.0.1"
    assert out["idn"].startswith("RIGOL")
    assert out["run_state"] == "STOP"
    assert out["capability"]["analog_bw_hz"] == pytest.approx(100.0e6)
    assert set(out["channels"]) == {"CH1", "CH2", "CH3", "CH4"}
    assert out["trigger"]["mode"] == "EDGE"
    assert out["trigger"]["params"]["source"] == "CHAN1"
    assert out["acquire"]["type"] == "NORM"
    # 4 channels displayed (fallback :DISP?→1) → sample-rate derating caveat.
    assert any("channel mode" in c for c in out["caveats"])
    assert "*IDN?" in t.queries
    # Read-only: no SCPI writes.
    assert t.writes == []


def test_doctor_save_writes_yaml(tmp_path: Path) -> None:
    drv, _ = _make_rigol()
    out = scope_cli.run_doctor(
        drv, host="10.0.0.1", save=True, data_dir=tmp_path
    )
    saved = sorted(tmp_path.glob("ds1104z_setup_*.yaml"))
    assert len(saved) == 1
    assert out["saved_to"] == str(saved[0])
    data = yaml.safe_load(saved[0].read_text(encoding="utf-8"))
    assert data["model"] == "RIGOL_DS1104Z"
    assert data["timebase"]["s_per_div"] == pytest.approx(1e-6)
    assert set(data["channels"]) == {"CH1", "CH2", "CH3", "CH4"}


def test_doctor_zds_snapshot(tmp_path: Path) -> None:
    """doctor is model-agnostic — same call path serves the ZDS driver."""
    responses = {
        ":ACQuire:TYPE?": "NORMal",
        ":ACQuire:AVERages?": "2",
        ":ACQuire:MDEPth?": "1400",
        ":ACQuire:SRATe?": "1.000000E+9",
        ":TRIGger:MODE?": "EDGE",
        ":TRIGger:SWEep?": "AUTO",
        ":TRIGger:HOLDoff?": "0",
        ":TRIGger:SOURce?": "CH1",
        ":TRIGger:SLOPe?": "POSitive",
        ":TRIGger:LEVel?": "0.000000E+0",
    }
    t = _RecordingTransport(response_map=responses)
    t._responses.setdefault(
        "*IDN?", "ZHIYUAN ELECT,ZDS1104,SN,V1.00,1.2.67"
    )
    drv = ZlgZds1000(transport=t, profile=_ZDS_PROFILE)
    drv.run_status_settle_s = 0
    out = scope_cli.run_doctor(
        drv, host="192.168.138.14", save=False, data_dir=tmp_path
    )
    assert out["model"] == "ZLG_ZDS1104"
    assert out["trigger"]["mode"] == "EDGE"
    assert out["capability"]["screenshot_formats"] == ["BMP"]
    assert out["acquire"]["type"] == "NORMal"


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------


def test_dump_rigol_normal_csv_and_meta(tmp_path: Path) -> None:
    drv, _ = _make_rigol(binary_response=_WAV_BYTES)
    out = scope_cli.run_dump(
        drv, channels=["CHAN1"], mode="NORMAL",
        host="10.0.0.1", data_dir=tmp_path,
    )
    assert out["schema"] == scope_cli.SCHEMA_VERSION
    assert out["mode"] == "NORMAL"
    assert len(out["files"]) == 1
    entry = out["files"][0]
    assert entry["channel"] == "CHAN1"
    assert entry["n_samples"] == 8

    csv_path = Path(entry["csv"])
    meta_path = Path(entry["meta"])
    assert csv_path.exists() and meta_path.exists()
    lines = csv_path.read_text(encoding="ascii").splitlines()
    assert lines[0] == "t_s,volts"
    assert len(lines) == 9  # header + 8 samples
    assert lines[1] == "0,0"
    assert lines[4] == "3e-06,3"  # t0=0, dt=1e-6, volts=3

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["schema"] == "scope-dump/1"
    assert meta["source"] == "CHAN1"
    assert meta["mode"] == "NORMAL"
    assert meta["n_samples"] == 8
    assert meta["dt_s"] == pytest.approx(1e-6)
    assert meta["csv"] == csv_path.name
    assert isinstance(meta["caveats"], list) and meta["caveats"]
    # Vertical context comes from the driver's channel queries.
    assert meta["scale_v_per_div"] == pytest.approx(0.0)


def test_dump_multi_channel(tmp_path: Path) -> None:
    drv, _ = _make_rigol(binary_response=_WAV_BYTES)
    out = scope_cli.run_dump(
        drv, channels=["CHAN1", "CHAN2"], mode="NORMAL",
        host="10.0.0.1", data_dir=tmp_path,
    )
    assert [f["channel"] for f in out["files"]] == ["CHAN1", "CHAN2"]
    for entry in out["files"]:
        assert Path(entry["csv"]).exists()
        assert Path(entry["meta"]).exists()


def test_main_dump_raw_not_stopped_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """RAW while running: the driver raises; main surfaces it and exits 1."""
    responses = dict(_RIGOL_FULL, **{":TRIG:STAT?": "RUN"})
    t = _RecordingTransport(response_map=responses)
    drv = RigolDs1000z(transport=t, profile=_RIGOL_PROFILE)
    drv.run_status_settle_s = 0
    monkeypatch.setattr(scope_cli, "open_scope", lambda **kw: drv)
    rc = scope_cli.main(["dump", "CHAN1", "--mode", "RAW",
                         "--data-dir", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "stopped" in err
    # Nothing written before the driver rejected the read.
    assert list(tmp_path.iterdir()) == []


def test_main_doctor_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    drv, _ = _make_rigol()
    monkeypatch.setattr(scope_cli, "open_scope", lambda **kw: drv)
    rc = scope_cli.main(["doctor", "--save", "--data-dir", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model"] == "RIGOL_DS1104Z"
    assert out["saved_to"].endswith(".yaml")
