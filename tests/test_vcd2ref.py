"""Unit tests for skills/scope-bench/scripts/vcd2ref.py — synthetic VCD
fixtures + dump-CSV input, no hardware and no external VCD library.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / (
    "skills/scope-bench/scripts/vcd2ref.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("vcd2ref", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vcd2ref"] = mod
    spec.loader.exec_module(mod)
    return mod


vcd2ref = _load_script()


_VCD = """\
$date 2026-09-18 $end
$timescale 1ns $end
$scope module top $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 1 " tx $end
$var wire 4 # bus $end
$upscope $end
$upscope $end
$enddefinitions $end
$comment simulation $end
#0
0!
x"
b0101 #
#100
1!
0"
#200
0!
1"
#300
1!
#400
0!
"""


@pytest.fixture
def vcd_file(tmp_path: Path) -> Path:
    p = tmp_path / "sim.vcd"
    p.write_text(_VCD, encoding="utf-8")
    return p


def test_vcd_full_name_and_short_name(vcd_file: Path, capsys) -> None:
    for signal in ("top.dut.clk", "clk"):
        rc = vcd2ref.main(["-i", str(vcd_file), "--signal", signal])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["schema"] == "vcd2ref/1"
        assert out["input_kind"] == "vcd"
        assert out["source"] == "top.dut.clk"
        assert out["timescale"] == "1ns"
        # #0 0! is the initial value (no edge); changes at 100..400 ns.
        assert out["edges"] == [
            [0.1, "RISE"], [0.2, "FALL"], [0.3, "RISE"], [0.4, "FALL"],
        ]


def test_vcd_ignores_x_and_vectors(vcd_file: Path, capsys) -> None:
    rc = vcd2ref.main(["-i", str(vcd_file), "--signal", "tx"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    # tx is x at t=0 (skipped — first definite level 0 at #100 becomes
    # the initial value, no edge), then rises at #200.
    assert out["edges"] == [[0.2, "RISE"]]


def test_vcd_timescale_with_space(tmp_path: Path, capsys) -> None:
    p = tmp_path / "ps.vcd"
    p.write_text(
        "$timescale 1 ps $end\n$var wire 1 ! s $end\n"
        "$enddefinitions $end\n#0\n0!\n#1000\n1!\n",
        encoding="utf-8",
    )
    rc = vcd2ref.main(["-i", str(p), "--signal", "s"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["edges"] == [[0.001, "RISE"]]  # 1000 ps = 1 ns = 0.001 µs


def test_vcd_ambiguous_short_name_fails(tmp_path: Path, capsys) -> None:
    p = tmp_path / "dup.vcd"
    p.write_text(
        "$timescale 1ns $end\n"
        "$scope module a $end\n$var wire 1 ! clk $end\n$upscope $end\n"
        "$scope module b $end\n$var wire 1 \" clk $end\n$upscope $end\n"
        "$enddefinitions $end\n",
        encoding="utf-8",
    )
    rc = vcd2ref.main(["-i", str(p), "--signal", "clk"])
    assert rc == 2
    assert "ambiguous" in capsys.readouterr().err


def test_vcd_unknown_signal_lists_candidates(
    vcd_file: Path, capsys
) -> None:
    rc = vcd2ref.main(["-i", str(vcd_file), "--signal", "nope"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err
    assert "top.dut.clk" in err  # candidates listed


def test_vcd_requires_signal_flag(vcd_file: Path, capsys) -> None:
    rc = vcd2ref.main(["-i", str(vcd_file)])
    assert rc == 2
    assert "--signal" in capsys.readouterr().err


def test_offset_shifts_axis(vcd_file: Path, capsys) -> None:
    rc = vcd2ref.main(["-i", str(vcd_file), "--signal", "clk",
                       "--offset-us", "-100.0"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["edges"][0] == [-99.9, "RISE"]
    assert out["offset_us"] == -100.0


def test_csv_input_quantizes(tmp_path: Path, capsys) -> None:
    csv = tmp_path / "hw_chan1.csv"
    csv.write_text(
        "t_s,volts\n"
        "0,0\n1e-06,0\n2e-06,0\n3e-06,3\n4e-06,3\n5e-06,3\n6e-06,0\n",
        encoding="ascii",
    )
    rc = vcd2ref.main(["-i", str(csv), "--threshold-v", "1.5",
                       "--hysteresis-v", "0.1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["input_kind"] == "csv"
    assert out["source"] == "hw_chan1"
    assert out["edges"] == [[3.0, "RISE"], [6.0, "FALL"]]
    assert out["quantized"]["threshold_v"] == 1.5


def test_out_file_written(vcd_file: Path, tmp_path: Path, capsys) -> None:
    out_path = tmp_path / "ref.json"
    rc = vcd2ref.main(["-i", str(vcd_file), "--signal", "clk",
                       "-o", str(out_path)])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["n_edges"] == 4
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert saved["edges"][0] == [0.1, "RISE"]
