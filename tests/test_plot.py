"""Unit tests for skills/scope-bench/scripts/plot.py — no hardware.

matplotlib IS installed in the dev env, so the PNG path is tested
directly (Agg backend). The HTML-fallback path is forced by poisoning
sys.modules['matplotlib'] with None, which makes ``import matplotlib``
raise ImportError inside the script.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / (
    "skills/scope-bench/scripts/plot.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("scope_plot", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scope_plot"] = mod
    spec.loader.exec_module(mod)
    return mod


plot = _load_script()

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _write_dump(tmp_path: Path, name: str, volts: list[float],
                dt: float = 1e-6) -> Path:
    csv = tmp_path / f"{name}.csv"
    rows = ["t_s,volts"]
    for i, v in enumerate(volts):
        rows.append(f"{-1e-3 + i * dt:.9g},{v:.6g}")
    csv.write_text("\n".join(rows) + "\n", encoding="ascii")
    (tmp_path / f"{name}.meta.json").write_text(json.dumps({
        "schema": "scope-dump/1", "source": name.upper(),
        "n_samples": len(volts), "t0_s": -1e-3, "dt_s": dt,
        "scale_v_per_div": 1.0, "offset_v": 0.0, "caveats": [],
    }), encoding="utf-8")
    return csv


def _write_measlog(tmp_path: Path) -> Path:
    csv = tmp_path / "measlog.csv"
    csv.write_text(
        "timestamp_s,VPP,FREQUENCY\n"
        "0.0,3.04,1000.1\n"
        "1.0,3.05,999.9\n"
        "2.0,,998.2\n"          # missing VPP → None
        "3.0,3.06,garbage\n"    # unmeasurable → None
        "4.0,3.03,1000.4\n",
        encoding="utf-8",
    )
    return csv


# ---------------------------------------------------------------------------
# wave
# ---------------------------------------------------------------------------


def test_wave_png_single_channel(tmp_path: Path, capsys) -> None:
    csv = _write_dump(tmp_path, "chan1", [0.0, 1.0, 3.0, 1.0] * 50)
    out = tmp_path / "wave.png"
    rc = plot.main(["wave", "-i", str(csv), "-o", str(out)])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backend"] == "matplotlib"
    assert result["channels"] == ["CHAN1"]
    assert out.read_bytes()[:8] == _PNG_MAGIC


def test_wave_png_multi_channel(tmp_path: Path, capsys) -> None:
    c1 = _write_dump(tmp_path, "chan1", [0.0, 3.0] * 50)
    c2 = _write_dump(tmp_path, "chan2", [3.0, 0.0] * 50)
    out = tmp_path / "multi.png"
    rc = plot.main(["wave", "-i", str(c1), str(c2), "-o", str(out)])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["channels"] == ["CHAN1", "CHAN2"]
    assert out.stat().st_size > 0


def test_wave_falls_back_to_html_without_matplotlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    csv = _write_dump(tmp_path, "chan1", [0.0, 3.0] * 50)
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    out = tmp_path / "wave.png"
    rc = plot.main(["wave", "-i", str(csv), "-o", str(out)])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    # Fallback wrote .html next to the requested .png name.
    html_path = Path(result["path"])
    assert html_path.suffix == ".html"
    assert result["backend"] == "viewer-html"
    assert any("matplotlib not installed" in c for c in result["caveats"])
    content = html_path.read_text(encoding="utf-8")
    assert "CHAN1" in content
    assert not out.exists()  # no PNG was written


def test_wave_missing_input_fails(tmp_path: Path, capsys) -> None:
    rc = plot.main(["wave", "-i", str(tmp_path / "nope.csv")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------


def test_trend_png(tmp_path: Path, capsys) -> None:
    csv = _write_measlog(tmp_path)
    out = tmp_path / "trend.png"
    rc = plot.main(["trend", "-i", str(csv), "-o", str(out)])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backend"] == "matplotlib"
    assert result["items"] == ["VPP", "FREQUENCY"]
    assert result["n_points"] == 5
    assert out.read_bytes()[:8] == _PNG_MAGIC


def test_trend_requires_matplotlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    csv = _write_measlog(tmp_path)
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    rc = plot.main(["trend", "-i", str(csv)])
    assert rc == 2
    assert "matplotlib" in capsys.readouterr().err


def test_trend_all_empty_columns_fail(tmp_path: Path, capsys) -> None:
    csv = tmp_path / "empty.csv"
    csv.write_text("timestamp_s,VPP\n0.0,\n1.0,\n", encoding="utf-8")
    rc = plot.main(["trend", "-i", str(csv)])
    assert rc == 2
    assert "plottable" in capsys.readouterr().err
