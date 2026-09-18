"""Unit tests for skills/scope-bench/scripts/analyze.py — fixtures in,
JSON/markdown out, no hardware and no MCP host.

The five sub-commands are thin bridges over helpers that already have
their own unit tests, so here we verify the bridge: lenient input
loading (capture JSON / hand-written runs JSON / dump CSV + meta),
channel selection, output shape, markdown rendering, and error paths.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / (
    "skills/scope-bench/scripts/analyze.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("analyze", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["analyze"] = mod
    spec.loader.exec_module(mod)
    return mod


analyze = _load_script()


# Two-channel synthetic capture: CHAN1 is a 20 µs square wave with two
# 5 µs short pulses grafted in; CHAN2 rises 2 µs after each CHAN1 rise
# except the last one (the causality violation).
_CAPTURE = {
    "schema": "capture/1",
    "triggered": True,
    "channels": ["CHAN1", "CHAN2"],
    "waveform": {
        "CHAN1": {
            "runs": [
                [-10.0, 0, 10.0], [0.0, 1, 10.0], [10.0, 0, 10.0],
                [20.0, 1, 10.0], [30.0, 0, 5.0], [35.0, 1, 5.0],
                [40.0, 0, 10.0],
            ],
            "edges": [
                [0.0, "CHAN1", "RISE"], [10.0, "CHAN1", "FALL"],
                [20.0, "CHAN1", "RISE"], [30.0, "CHAN1", "FALL"],
                [35.0, "CHAN1", "RISE"], [40.0, "CHAN1", "FALL"],
            ],
            "n_samples": 1200,
        },
        "CHAN2": {
            "runs": [
                [-10.0, 0, 12.0], [2.0, 1, 8.0], [10.0, 0, 12.0],
                [22.0, 1, 8.0], [30.0, 0, 20.0],
            ],
            "edges": [
                [2.0, "CHAN2", "RISE"], [10.0, "CHAN2", "FALL"],
                [22.0, "CHAN2", "RISE"], [30.0, "CHAN2", "FALL"],
            ],
            "n_samples": 1200,
        },
    },
    "bus_runs": None,
    "measurements": None,
    "caveats": ["synthetic fixture caveat"],
}


@pytest.fixture
def capture_file(tmp_path: Path) -> Path:
    p = tmp_path / "capture.json"
    p.write_text(json.dumps(_CAPTURE), encoding="utf-8")
    return p


@pytest.fixture
def dump_files(tmp_path: Path) -> Path:
    """scope_cli dump output: t_s,volts CSV + sibling meta.json."""
    csv = tmp_path / "ds1104z_waveform_20260918_120000_chan1.csv"
    csv.write_text(
        "t_s,volts\n"
        "0,0\n1e-06,0\n2e-06,0\n3e-06,3\n4e-06,3\n5e-06,3\n6e-06,0\n7e-06,0\n",
        encoding="ascii",
    )
    meta = tmp_path / (csv.stem + ".meta.json")
    meta.write_text(json.dumps({
        "schema": "scope-dump/1",
        "source": "CHAN1",
        "mode": "NORMal",
        "n_samples": 8,
        "t0_s": 0.0,
        "dt_s": 1e-6,
        "scale_v_per_div": 1.0,
        "offset_v": 0.0,
        "caveats": ["dump caveat"],
    }), encoding="utf-8")
    return csv


# ---------------------------------------------------------------------------
# input loading
# ---------------------------------------------------------------------------


def test_load_capture_json(capture_file: Path) -> None:
    inp = analyze.load_input(capture_file, threshold_v=1.5, hysteresis_v=0.1)
    assert set(inp) == {"CHAN1", "CHAN2"}
    ch1 = inp["CHAN1"]
    assert len(ch1["runs"]) == 7
    assert ch1["runs"][1].level == 1
    assert ch1["edges"][0].kind == "RISE"
    # Top-level capture caveats are inherited per channel.
    assert "synthetic fixture caveat" in ch1["caveats"]


def test_load_handwritten_runs_json(tmp_path: Path) -> None:
    p = tmp_path / "manual.json"
    p.write_text(json.dumps({
        "runs": [[0.0, 0, 5.0], [5.0, 1, 5.0]],
        "edges": [[5.0, "RISE"]],  # compare-style 2-tuple
    }), encoding="utf-8")
    inp = analyze.load_input(p, threshold_v=1.5, hysteresis_v=0.1)
    assert set(inp) == {"CHAN1"}
    assert inp["CHAN1"]["edges"][0].channel == "CHAN1"


def test_load_dump_csv_requantizes(dump_files: Path) -> None:
    inp = analyze.load_input(dump_files, threshold_v=1.5, hysteresis_v=0.1)
    ch1 = inp["CHAN1"]
    assert len(ch1["runs"]) == 3  # low(3) high(3) low(2)
    assert ch1["runs"][1] == (3.0, 1, 3.0)
    assert "dump caveat" in ch1["caveats"]
    assert any("re-quantized" in c for c in ch1["caveats"])


def test_load_rejects_shapeless_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="runs"):
        analyze.load_input(p, threshold_v=1.5, hysteresis_v=0.1)


# ---------------------------------------------------------------------------
# sub-commands via main()
# ---------------------------------------------------------------------------


def test_main_glitch(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "glitch",
                       "--min-width-us", "7"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "scope-analyze/1"
    assert out["command"] == "glitch"
    assert out["channel"] == "CHAN1"  # first channel by default
    assert [g["t_us"] for g in out["glitches"]] == [30.0, 35.0]
    assert "synthetic fixture caveat" in out["caveats"]


def test_main_jitter(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "jitter", "--kind", "RISE"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    stats = out["stats"]
    # RISE edges at 0, 20, 35 → intervals [20, 15].
    assert stats["count"] == 2
    assert stats["mean_us"] == pytest.approx(17.5)
    assert stats["min_us"] == pytest.approx(15.0)
    assert stats["max_us"] == pytest.approx(20.0)
    assert len(stats["histogram"]) == 10


def test_main_pattern(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "pattern",
                       "--pattern", "0,1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [m["index"] for m in out["matches"]] == [0, 2, 4]
    assert [m["t_us"] for m in out["matches"]] == [-10.0, 10.0, 30.0]


def test_main_causality(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "causality",
                       "--a", "CHAN1", "--b", "CHAN2",
                       "--max-delay-us", "3"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_a"] == 3      # RISE at 0, 20, 35
    assert out["matched"] == 2      # 0→2, 20→22
    assert out["violations"] == 1   # 35 has no following B rise
    v = out["violation_details"][0]
    assert v["a_t_us"] == pytest.approx(35.0)
    assert v["nearest_b_t_us"] is None


def test_main_bus(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "bus",
                       "--channels", "CHAN1,CHAN2"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["channel_order_msb_first"] == ["CHAN1", "CHAN2"]
    values = [r[1] for r in out["bus_runs"]]
    # Segments at boundary union -10,0,2,10,20,22,30,35,40:
    # CH1/CH2 levels pack to 00,10,11,00,10,11,00,10,00.
    assert values == [0, 2, 3, 0, 2, 3, 0, 2, 0]


def test_main_dump_csv_input(dump_files: Path, capsys) -> None:
    rc = analyze.main(["-i", str(dump_files), "jitter", "--kind", "RISE"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    # Single RISE edge → zero intervals, but the command must succeed.
    assert out["stats"]["count"] == 0
    assert any("re-quantized" in c for c in out["caveats"])


def test_main_markdown(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "--md", "glitch",
                       "--min-width-us", "7"])
    assert rc == 0
    md = capsys.readouterr().out
    assert md.startswith("# scope-bench analyze — glitch")
    assert "| t_us | level | dur_us | run_index |" in md
    assert "- synthetic fixture caveat" in md


def test_main_bus_markdown_binary_column(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "--md", "bus",
                       "--channels", "CHAN1,CHAN2"])
    assert rc == 0
    md = capsys.readouterr().out
    assert "| 0b10 |" in md  # 2-bit rendering of value 2


def test_main_unknown_channel_fails(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "glitch",
                       "--min-width-us", "7", "--channel", "CHAN9"])
    assert rc == 2
    assert "CHAN9" in capsys.readouterr().err


def test_main_missing_input_fails(tmp_path: Path, capsys) -> None:
    rc = analyze.main(["-i", str(tmp_path / "nope.json"), "jitter"])
    assert rc == 2


# ---------------------------------------------------------------------------
# volts-domain sub-commands (fft / hist / envelope) — dump CSV only
# ---------------------------------------------------------------------------


import math  # noqa: E402


@pytest.fixture
def sine_csv(tmp_path: Path) -> Path:
    """Pure 1 kHz sine, 0.5 V amplitude, 1024 samples @ 100 kSa/s (no DC
    offset — a DC offset would leak past the skipped bin 0 and dominate
    the peak list)."""
    csv = tmp_path / "sine_chan1.csv"
    n, dt = 1024, 1e-5
    rows = ["t_s,volts"]
    for i in range(n):
        rows.append(f"{i * dt:.9g},{0.5 * math.sin(2 * math.pi * 1000 * i * dt):.6g}")
    csv.write_text("\n".join(rows) + "\n", encoding="ascii")
    (tmp_path / "sine_chan1.meta.json").write_text(json.dumps({
        "schema": "scope-dump/1", "source": "CHAN1",
        "n_samples": n, "t0_s": 0.0, "dt_s": dt,
        "caveats": ["sine fixture caveat"],
    }), encoding="utf-8")
    return csv


@pytest.fixture
def bimodal_csv(tmp_path: Path) -> Path:
    """Digital-ish levels: 401 samples @ 0 V, 200 @ 1.65 V, 399 @ 3.3 V —
    the odd lengths place both transitions strictly inside an envelope
    block (block_size 10), so straddling blocks keep both extremes."""
    csv = tmp_path / "levels_chan1.csv"
    volts = [0.0] * 401 + [1.65] * 200 + [3.3] * 399
    rows = ["t_s,volts"]
    for i, v in enumerate(volts):
        rows.append(f"{i * 1e-6:.9g},{v:.6g}")
    csv.write_text("\n".join(rows) + "\n", encoding="ascii")
    (tmp_path / "levels_chan1.meta.json").write_text(json.dumps({
        "schema": "scope-dump/1", "source": "CHAN1",
        "n_samples": len(volts), "t0_s": 0.0, "dt_s": 1e-6, "caveats": [],
    }), encoding="utf-8")
    return csv


def test_main_fft_finds_tone(sine_csv: Path, capsys) -> None:
    rc = analyze.main(["-i", str(sine_csv), "fft", "--peaks", "3"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "fft"
    assert out["source"] == "CHAN1"
    top = out["result"]["peaks"][0]
    assert 900 <= top["freq_hz"] <= 1100  # the 1 kHz tone
    assert top["magnitude_db"] == pytest.approx(0.0)  # reference peak
    assert "sine fixture caveat" in out["caveats"]


def test_main_fft_zero_pad_caveat(tmp_path: Path, capsys) -> None:
    csv = tmp_path / "odd.csv"
    csv.write_text("t_s,volts\n" +
                   "".join(f"{i * 1e-5:.9g},{math.sin(i)}\n" for i in range(100)),
                   encoding="ascii")
    rc = analyze.main(["-i", str(csv), "fft"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert any("zero-padded 100" in c for c in out["caveats"])


def test_main_hist_bimodal_levels(bimodal_csv: Path, capsys) -> None:
    rc = analyze.main(["-i", str(bimodal_csv), "hist", "--bins", "20"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    r = out["result"]
    assert r["min_v"] == pytest.approx(0.0)
    assert r["max_v"] == pytest.approx(3.3)
    centers = sorted(p["center_v"] for p in r["peaks"])
    assert len(centers) >= 2
    assert centers[0] == pytest.approx(0.0, abs=0.5)
    assert centers[-1] == pytest.approx(3.3, abs=0.5)


def test_main_envelope_blocks(bimodal_csv: Path, capsys) -> None:
    rc = analyze.main(["-i", str(bimodal_csv), "envelope", "--points", "100"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    r = out["result"]
    assert r["n_blocks"] == 100
    assert r["block_size"] == 10
    # Block straddling the 0→1.65 transition keeps both extremes.
    straddle = [e for e in r["envelope"]
                if e["min_v"] == pytest.approx(0.0)
                and e["max_v"] == pytest.approx(1.65)]
    assert straddle


def test_volts_commands_reject_json(capture_file: Path, capsys) -> None:
    rc = analyze.main(["-i", str(capture_file), "fft"])
    assert rc == 2
    assert "dump CSV" in capsys.readouterr().err


def test_main_fft_markdown(sine_csv: Path, capsys) -> None:
    rc = analyze.main(["-i", str(sine_csv), "--md", "fft"])
    assert rc == 0
    md = capsys.readouterr().out
    assert "# scope-bench analyze — fft" in md
    assert "| freq_hz | magnitude_db |" in md
