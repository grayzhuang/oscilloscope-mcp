"""plot — render captured scope data to PNG (offline, file in / file out).

Sub-commands
------------
wave   One or more dump CSVs (multi-channel) → waveform PNG with a
       trigger-point marker at t = 0. If matplotlib is missing, falls
       back to the zero-dependency interactive HTML viewer
       (``oscilloscope_mcp.helpers.viewer``) — the repo core stays
       third-party-free; matplotlib is an optional extra
       (``pip install '.[plot]'``).
trend  A meas-log CSV (``timestamp_s,<item>,…``) → per-item trend PNG.
       No fallback: trends need matplotlib; install it or read the CSV.

Usage::

    python skills/scope-bench/scripts/plot.py wave -i dump_ch1.csv dump_ch2.csv -o data/wave.png
    python skills/scope-bench/scripts/plot.py trend -i data/<model>_measlog_<ts>.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "scope-plot/1"

# Distinct plot colors, cycled per channel (same palette the HTML viewer
# uses for CHAN1..4).
_COLORS = ["#ffca28", "#4fc3f7", "#ff4081", "#2979ff", "#66bb6a", "#ab47bc"]


def _read_samples_csv(csv_path: Path) -> tuple[list[float], list[float]]:
    times: list[float] = []
    volts: list[float] = []
    with csv_path.open("r", encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("t_s", "timestamp_s")):
                continue
            t, v = line.split(",")
            times.append(float(t))
            volts.append(float(v))
    return times, volts


def _csv_meta(csv_path: Path) -> dict[str, Any]:
    meta_path = csv_path.with_name(csv_path.stem + ".meta.json")
    if meta_path.is_file():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    return {}


# ---------------------------------------------------------------------------
# wave
# ---------------------------------------------------------------------------


def _render_wave_html(inputs: list[Path], out_path: Path, title: str) -> dict:
    """matplotlib-less fallback: reuse the interactive HTML viewer."""
    from oscilloscope_mcp.helpers.viewer import generate_viewer_html

    channels: dict[str, dict[str, Any]] = {}
    for i, csv_path in enumerate(inputs):
        meta = _csv_meta(csv_path)
        times, volts = _read_samples_csv(csv_path)
        if not volts:
            raise ValueError(f"no samples found in {csv_path}")
        t0 = times[0]
        dt = (times[1] - times[0]) if len(times) > 1 else 1.0
        name = str(meta.get("source") or csv_path.stem)
        # Suffix duplicates so the viewer's per-channel dict keeps both.
        if name in channels:
            name = f"{name}#{i + 1}"
        channels[name] = {
            "volts": volts,
            "t0_us": t0 * 1e6,
            "dt_us": dt * 1e6,
            "n_samples": len(volts),
            "scale_v_per_div": meta.get("scale_v_per_div"),
            "offset_v": meta.get("offset_v"),
        }
    html = generate_viewer_html(channels, title=title)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return {
        "path": str(out_path),
        "backend": "viewer-html",
        "channels": sorted(channels),
        "bytes_written": len(html.encode("utf-8")),
    }


def _render_wave_png(inputs: list[Path], out_path: Path, title: str) -> dict:
    import matplotlib

    matplotlib.use("Agg")  # headless: this CLI never has a display
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5), dpi=150)
    for i, csv_path in enumerate(inputs):
        meta = _csv_meta(csv_path)
        times, volts = _read_samples_csv(csv_path)
        if not volts:
            raise ValueError(f"no samples found in {csv_path}")
        label = str(meta.get("source") or csv_path.stem)
        ax.plot(times, volts, lw=0.6,
                color=_COLORS[i % len(_COLORS)], label=label)
    ax.axvline(0.0, color="red", lw=0.8, ls="--", alpha=0.7,
               label="trigger (t=0)")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("voltage (V)")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    return {
        "path": str(out_path),
        "backend": "matplotlib",
        "channels": [
            str(_csv_meta(p).get("source") or p.stem) for p in inputs
        ],
        "bytes_written": out_path.stat().st_size,
    }


def run_wave(inputs: list[Path], out: Path | None, title: str) -> dict:
    out_path = out or Path(f"wave_{inputs[0].stem}.png")
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        fallback = out_path.with_suffix(".html")
        result = _render_wave_html(inputs, fallback, title)
        result["caveats"] = [
            "matplotlib not installed — rendered the interactive HTML "
            "viewer instead of a PNG (pip install '.[plot]' for PNG)."
        ]
        return result
    return _render_wave_png(inputs, out_path, title)


# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------


def _read_measlog_csv(path: Path) -> tuple[list[str], dict[str, list[float | None]]]:
    """meas-log CSV → (item names, {item: [values]}); timestamp kept as
    its own series under ``t``. Empty cells / non-numeric → None."""
    items: list[str] = []
    series: dict[str, list[float | None]] = {}
    with path.open("r", encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        items = [h.strip() for h in header[1:]]
        for name in ["t", *items]:
            series[name] = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            series["t"].append(float(parts[0]))
            for name, raw in zip(items, parts[1:]):
                try:
                    series[name].append(float(raw) if raw.strip() else None)
                except ValueError:
                    series[name].append(None)
    return items, series


def run_trend(inp: Path, out: Path | None, title: str) -> dict:
    try:
        import matplotlib
    except ImportError:
        raise ValueError(
            "trend needs matplotlib (optional extra: pip install '.[plot]'); "
            "no HTML fallback exists for trend charts"
        )

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    items, series = _read_measlog_csv(inp)
    plottable = [i for i in items if any(v is not None for v in series[i])]
    if not plottable:
        raise ValueError(f"no plottable measurement columns in {inp}")

    out_path = out or Path(f"trend_{inp.stem}.png")
    fig, axes = plt.subplots(
        len(plottable), 1, figsize=(12, 2.6 * len(plottable)), dpi=150,
        sharex=True, squeeze=False,
    )
    for ax, item in zip(axes[:, 0], plottable):
        pts = [(t, v) for t, v in zip(series["t"], series[item])
               if v is not None]
        ax.plot([p[0] for p in pts], [p[1] for p in pts],
                lw=1.0, color=_COLORS[0], marker=".", ms=3)
        ax.set_ylabel(item, fontsize=9)
        ax.grid(True, alpha=0.3)
        vals = [p[1] for p in pts]
        ax.set_ylim(min(vals) - 0.05 * (max(vals) - min(vals) or 1),
                    max(vals) + 0.05 * (max(vals) - min(vals) or 1))
    axes[-1, 0].set_xlabel("elapsed (s)")
    fig.suptitle(title)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    return {
        "path": str(out_path),
        "backend": "matplotlib",
        "items": plottable,
        "n_points": len(series["t"]),
        "bytes_written": out_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# CLI assembly
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plot",
        description=(
            "Render scope captures to PNG (waveform / measurement trend). "
            "matplotlib is optional; wave falls back to the HTML viewer."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_wave = sub.add_parser(
        "wave", help="one or more dump CSVs → waveform PNG (or HTML fallback)"
    )
    p_wave.add_argument("-i", "--input", nargs="+", required=True,
                        help="dump CSV(s) — one curve each")
    p_wave.add_argument("-o", "--out", default=None, help="output PNG path")
    p_wave.add_argument("--title", default="Waveform", help="plot title")

    p_trend = sub.add_parser(
        "trend", help="meas-log CSV → per-item trend PNG"
    )
    p_trend.add_argument("-i", "--input", required=True, help="meas-log CSV")
    p_trend.add_argument("-o", "--out", default=None, help="output PNG path")
    p_trend.add_argument("--title", default="Measurement trend",
                         help="plot title")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "wave":
            inputs = [Path(p) for p in args.input]
            missing = [str(p) for p in inputs if not p.is_file()]
            if missing:
                raise ValueError(f"input file(s) not found: {missing}")
            out = run_wave(inputs, Path(args.out) if args.out else None,
                           args.title)
        else:
            inp = Path(args.input)
            if not inp.is_file():
                raise ValueError(f"input file not found: {inp}")
            out = run_trend(inp, Path(args.out) if args.out else None,
                            args.title)
        out["schema"] = SCHEMA_VERSION
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
