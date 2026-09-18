"""Unit tests for skills/scope-bench/scripts/compare_rtl.py — ref/hw in
many shapes, diff correctness, markdown report; all offline.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / (
    "skills/scope-bench/scripts/compare_rtl.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("compare_rtl", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["compare_rtl"] = mod
    spec.loader.exec_module(mod)
    return mod


compare_rtl = _load_script()


# Reference: ideal square wave, edges at 0/10/20/30 µs.
_REF = {
    "schema": "vcd2ref/1", "source": "top.dut.tx",
    "edges": [[0.0, "RISE"], [10.0, "FALL"], [20.0, "RISE"], [30.0, "FALL"]],
}

# Capture JSON: CHAN1 carries the ref's 0/10/30 µs edges each +0.02 µs
# skew, is missing the 20 µs RISE, and adds an extra 15 µs RISE.
_HW_CAPTURE = {
    "schema": "capture/1",
    "waveform": {
        "CHAN1": {
            "runs": [[-10.0, 0, 10.02], [0.02, 1, 10.0], [10.02, 0, 4.98],
                     [15.0, 1, 15.02], [30.02, 0, 9.98]],
            "edges": [[0.02, "CHAN1", "RISE"], [10.02, "CHAN1", "FALL"],
                      [15.0, "CHAN1", "RISE"], [30.02, "CHAN1", "FALL"]],
            "n_samples": 1200,
        },
        "CHAN2": {
            "runs": [[-10.0, 0, 50.0]],
            "edges": [],
            "n_samples": 1200,
        },
    },
    "caveats": ["capture caveat"],
}


@pytest.fixture
def ref_file(tmp_path: Path) -> Path:
    p = tmp_path / "ref.json"
    p.write_text(json.dumps(_REF), encoding="utf-8")
    return p


@pytest.fixture
def hw_capture(tmp_path: Path) -> Path:
    p = tmp_path / "capture.json"
    p.write_text(json.dumps(_HW_CAPTURE), encoding="utf-8")
    return p


def test_diff_shapes(ref_file: Path, hw_capture: Path, capsys) -> None:
    rc = compare_rtl.main(["--ref", str(ref_file), "--hw", str(hw_capture),
                           "--tolerance-us", "0.1"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["schema"] == "compare-rtl/1"
    # 0.02 RISE, 10.02 FALL, 30.02 FALL match the ref's 0/10/30 edges.
    assert rep["matched"] == 3
    deltas = {s["kind"]: s["delta_us"] for s in rep["shifted"]}
    assert deltas["RISE"] == pytest.approx(0.02)
    assert deltas["FALL"] == pytest.approx(0.02)
    # ref's 20 µs RISE absent on hw → missing; hw's 15 µs RISE extra.
    assert [m["t_us"] for m in rep["missing"]] == [20.0]
    assert [(a["t_us"], a["kind"]) for a in rep["added"]] == [(15.0, "RISE")]
    # First divergence = earliest of missing ∪ added → the 15 µs added.
    assert rep["first_divergence_us"] == pytest.approx(15.0)
    assert "capture caveat" in rep["caveats"]


def test_hw_channel_selection(
    ref_file: Path, hw_capture: Path, capsys
) -> None:
    rc = compare_rtl.main(["--ref", str(ref_file), "--hw", str(hw_capture),
                           "--hw-channel", "CHAN2"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["matched"] == 0
    assert len(rep["missing"]) == 4  # flat CHAN2 has no edges at all


def test_unknown_hw_channel_fails(
    ref_file: Path, hw_capture: Path, capsys
) -> None:
    rc = compare_rtl.main(["--ref", str(ref_file), "--hw", str(hw_capture),
                           "--hw-channel", "CHAN9"])
    assert rc == 2
    assert "CHAN9" in capsys.readouterr().err


def test_ref_runs_document(tmp_path: Path, hw_capture: Path, capsys) -> None:
    p = tmp_path / "ref_runs.json"
    p.write_text(json.dumps({
        "runs": [[0.0, 0, 0.0], [0.0, 1, 10.0], [10.0, 0, 10.0],
                 [20.0, 1, 10.0], [30.0, 0, 10.0]],
    }), encoding="utf-8")
    rc = compare_rtl.main(["--ref", str(p), "--hw", str(hw_capture),
                           "--tolerance-us", "0.1"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["matched"] == 3  # same diff as the edges-form reference


def test_hw_dump_csv_input(
    ref_file: Path, tmp_path: Path, capsys
) -> None:
    csv = tmp_path / "hw_chan1.csv"
    rows = ["t_s,volts"]
    # 5 µs low, 5 µs high, 5 µs low, 5 µs high, 5 µs low @ 1 µs samples.
    volts = [0.0] * 5 + [3.0] * 5 + [0.0] * 5 + [3.0] * 5 + [0.0] * 5
    for i, v in enumerate(volts):
        rows.append(f"{i * 1e-6:.9g},{v:.6g}")
    csv.write_text("\n".join(rows) + "\n", encoding="ascii")
    (tmp_path / "hw_chan1.meta.json").write_text(json.dumps({
        "source": "CHAN1", "t0_s": 0.0, "dt_s": 1e-6, "caveats": [],
    }), encoding="utf-8")

    rc = compare_rtl.main(["--ref", str(ref_file), "--hw", str(csv),
                           "--threshold-v", "1.5", "--hysteresis-v", "0.1",
                           "--tolerance-us", "0.5"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    # HW edges at 5/10/15/20 µs vs ref at 0/10/20/30: only 10 µs FALL
    # matches within 0.5 µs; everything else diverges.
    assert rep["matched"] == 1
    assert any("re-quantized" in c for c in rep["caveats"])


def test_offset_aligns_and_rematches(
    ref_file: Path, hw_capture: Path, capsys
) -> None:
    rc = compare_rtl.main(["--ref", str(ref_file), "--hw", str(hw_capture),
                           "--ref-offset-us", "-0.02", "--tolerance-us", "0.1"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    # Shifting ref by the measured skew: the three existing edges match;
    # the 20 µs RISE is genuinely absent on hw (stays missing at 19.98)
    # and the hw's 15 µs RISE stays added.
    assert rep["matched"] == 3
    assert [m["t_us"] for m in rep["missing"]] == [19.98]


def test_markdown_report(
    ref_file: Path, hw_capture: Path, tmp_path: Path, capsys
) -> None:
    md = tmp_path / "report.md"
    rc = compare_rtl.main(["--ref", str(ref_file), "--hw", str(hw_capture),
                           "--tolerance-us", "0.1", "--md", str(md)])
    assert rc == 0
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# RTL vs hardware")
    assert "## Missing (in sim, not on HW)" in text
    assert "## Added (on HW, not in sim)" in text
    assert "- capture caveat" in text
    rep = json.loads(capsys.readouterr().out)
    assert rep["markdown_report"] == str(md)


def test_missing_inputs_fail(tmp_path: Path, capsys) -> None:
    rc = compare_rtl.main(["--ref", str(tmp_path / "nope.json"),
                           "--hw", str(tmp_path / "nope2.json")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err
