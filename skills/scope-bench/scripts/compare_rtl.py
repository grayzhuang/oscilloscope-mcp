"""compare_rtl — RTL reference vs hardware capture, offline diff report.

The scripted counterpart of the ``scope_compare`` MCP tool: takes a
reference produced by ``vcd2ref.py`` (or hand-written runs/edges JSON)
and a hardware capture (``scope_capture`` result JSON, a dump CSV that
gets re-quantized, or hand-written edges JSON), runs
``helpers.reference_diff`` and prints JSON — or ``--md`` writes a
markdown report (matched / shifted with per-edge delta / missing /
added / first divergence / caveats).

Time-axis alignment is the caller's job: hardware edges are
trigger-aligned (t=0 is the trigger point), the simulation axis is
whatever the testbench used. Align with ``vcd2ref --offset-us`` (shift
the reference) or ``--hw-offset-us`` here (shift the hardware side).

Usage::

    python skills/scope-bench/scripts/compare_rtl.py \\
        --ref ref.json --hw capture.json --hw-channel CHAN1 \\
        --tolerance-us 0.05 --md report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from oscilloscope_mcp.helpers import edges as edges_helper
from oscilloscope_mcp.helpers import quantize as quantize_helper
from oscilloscope_mcp.helpers import rle as rle_helper
from oscilloscope_mcp.helpers import reference_diff as refdiff_helper
from oscilloscope_mcp.helpers.rle import Run

SCHEMA_VERSION = "compare-rtl/1"


# ---------------------------------------------------------------------------
# Edge-list builders (each input shape → list[Edge])
# ---------------------------------------------------------------------------


def _edges_from_pairs(raw: list, channel: str) -> list[edges_helper.Edge]:
    """[[t_us, kind]] or [[t_us, channel, kind]] → Edge list."""
    out: list[edges_helper.Edge] = []
    for e in raw:
        if len(e) >= 3:
            out.append(edges_helper.Edge(float(e[0]), str(e[1]), str(e[2])))
        else:
            out.append(edges_helper.Edge(float(e[0]), channel, str(e[1])))
    return out


def _runs_from_json(raw: list) -> list[Run]:
    return [Run(float(r[0]), int(r[1]), float(r[2])) for r in raw]


def load_ref(path: Path) -> tuple[list[edges_helper.Edge], list[str]]:
    """Reference: vcd2ref JSON, or hand-written {runs}/{edges} document."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: reference must be a JSON object")
    caveats = list(data.get("caveats") or [])
    if data.get("edges"):
        source = str(data.get("source") or "REF")
        return _edges_from_pairs(data["edges"], source), caveats
    if data.get("runs"):
        return (
            refdiff_helper.runs_to_edges(_runs_from_json(data["runs"]), "REF"),
            caveats,
        )
    raise ValueError(
        f"{path.name}: no 'edges' or 'runs' found in reference document"
    )


def load_hw(
    path: Path,
    *,
    channel: str | None,
    threshold_v: float,
    hysteresis_v: float,
) -> tuple[list[edges_helper.Edge], list[str]]:
    """Hardware: capture JSON (waveform.CHANn), dump CSV (re-quantized),
    or hand-written {runs}/{edges} document."""
    if path.suffix.lower() == ".csv":
        return _hw_from_csv(path, threshold_v=threshold_v,
                            hysteresis_v=hysteresis_v)

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: hardware input must be a JSON object")

    wf = data.get("waveform")
    if isinstance(wf, dict) and wf:
        keys = sorted(wf)
        key = channel or keys[0]
        if key not in wf:
            raise ValueError(
                f"hw channel {key!r} not in capture (found: {keys}); "
                "pass --hw-channel"
            )
        body = wf[key]
        caveats = list(data.get("caveats") or []) + list(
            body.get("caveats") or []
        )
        if body.get("edges"):
            return _edges_from_pairs(body["edges"], key), caveats
        return (
            refdiff_helper.runs_to_edges(
                _runs_from_json(body.get("runs") or []), key
            ),
            caveats,
        )

    if data.get("edges") or data.get("runs"):
        caveats = list(data.get("caveats") or [])
        if data.get("edges"):
            return _edges_from_pairs(data["edges"], "HW"), caveats
        return refdiff_helper.runs_to_edges(
            _runs_from_json(data["runs"]), "HW"
        ), caveats

    raise ValueError(
        f"{path.name}: no 'waveform', 'runs' or 'edges' found — expected a "
        "scope_capture result, a dump CSV, or an edges/runs document"
    )


def _hw_from_csv(
    path: Path, *, threshold_v: float, hysteresis_v: float
) -> tuple[list[edges_helper.Edge], list[str]]:
    times: list[float] = []
    volts: list[float] = []
    with path.open("r", encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("t_s"):
                continue
            t, v = line.split(",")
            times.append(float(t))
            volts.append(float(v))
    if not volts:
        raise ValueError(f"no samples found in {path}")

    meta: dict[str, Any] = {}
    meta_path = path.with_name(path.stem + ".meta.json")
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    t0 = float(meta.get("t0_s", times[0]))
    dt = float(meta.get("dt_s", 0.0)) or (
        (times[1] - times[0]) if len(times) > 1 else 1.0
    )
    levels = quantize_helper.quantize(volts, threshold_v, hysteresis_v)
    runs = rle_helper.runs_from_levels(levels, t0_us=t0 * 1e6, dt_us=dt * 1e6)
    source = str(meta.get("source", path.stem))
    caveats = list(meta.get("caveats") or [])
    caveats.append(
        f"hw re-quantized offline: threshold_v={threshold_v}, "
        f"hysteresis_v={hysteresis_v}; edge resolution is one {dt:.3g} s "
        "sample interval"
    )
    return edges_helper.edges_from_runs(runs, source), caveats


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(
    diff: dict[str, Any], *, ref: str, hw: str, tolerance_us: float,
    ref_offset_us: float, hw_offset_us: float, caveats: list[str],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ref": ref,
        "hw": hw,
        "tolerance_us": tolerance_us,
        "ref_offset_us": ref_offset_us,
        "hw_offset_us": hw_offset_us,
        **diff,
        "caveats": caveats,
    }


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [
        "# RTL vs hardware — edge diff",
        "",
        f"* reference: `{rep['ref']}`"
        + (f" (shifted {rep['ref_offset_us']} µs)" if rep["ref_offset_us"] else ""),
        f"* hardware: `{rep['hw']}`"
        + (f" (shifted {rep['hw_offset_us']} µs)" if rep["hw_offset_us"] else ""),
        f"* tolerance: {rep['tolerance_us']} µs",
        "",
        f"**{rep['summary']}**",
        "",
    ]
    if rep["first_divergence_us"] is not None:
        lines.append(f"First divergence: **{rep['first_divergence_us']} µs**")
        lines.append("")

    if rep["shifted"]:
        lines += ["## Matched edges", "",
                  f"{len(rep['shifted'])} edge(s) matched within tolerance:",
                  "",
                  "| ref t_us | hw t_us | Δus | kind |",
                  "|---|---|---|---|"]
        for s in rep["shifted"]:
            lines.append(
                f"| {s['ref_t_us']} | {s['hw_t_us']} | "
                f"{s['delta_us']:+} | {s['kind']} |"
            )
        lines.append("")

    if rep["missing"]:
        lines += ["## Missing (in sim, not on HW)", "",
                  "| t_us | kind |", "|---|---|"]
        for m in rep["missing"]:
            lines.append(f"| {m['t_us']} | {m['kind']} |")
        lines.append("")

    if rep["added"]:
        lines += ["## Added (on HW, not in sim)", "",
                  "| t_us | kind |", "|---|---|"]
        for a in rep["added"]:
            lines.append(f"| {a['t_us']} | {a['kind']} |")
        lines.append("")

    if rep.get("caveats"):
        lines += ["## Caveats", ""]
        lines += [f"- {c}" for c in rep["caveats"]]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare_rtl",
        description=(
            "Diff a simulator reference against a hardware capture "
            "(offline; no scope connection)."
        ),
    )
    parser.add_argument("--ref", required=True,
                        help="reference JSON (vcd2ref output or runs/edges)")
    parser.add_argument(
        "--hw", required=True,
        help="hardware: scope_capture JSON, dump CSV, or runs/edges JSON",
    )
    parser.add_argument("--hw-channel", default=None,
                        help="channel key inside a capture JSON (default: first)")
    parser.add_argument("--tolerance-us", type=float, default=0.05,
                        help="match tolerance in µs (default 0.05 = 50 ns)")
    parser.add_argument("--ref-offset-us", type=float, default=0.0,
                        help="shift reference edges (µs)")
    parser.add_argument("--hw-offset-us", type=float, default=0.0,
                        help="shift hardware edges (µs)")
    parser.add_argument("--threshold-v", type=float, default=1.5,
                        help="hw dump-CSV quantization threshold (V)")
    parser.add_argument("--hysteresis-v", type=float, default=0.1,
                        help="hw dump-CSV quantization hysteresis (V)")
    parser.add_argument("--md", default=None, metavar="REPORT.md",
                        help="also write a markdown report to this path")
    return parser


def _shift(
    edges: list[edges_helper.Edge], offset_us: float
) -> list[edges_helper.Edge]:
    if not offset_us:
        return edges
    return [
        edges_helper.Edge(round(e.t_us + offset_us, 6), e.channel, e.kind)
        for e in edges
    ]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ref_path, hw_path = Path(args.ref), Path(args.hw)
        for p, role in ((ref_path, "ref"), (hw_path, "hw")):
            if not p.is_file():
                raise ValueError(f"{role} input file not found: {p}")
        ref_edges, ref_caveats = load_ref(ref_path)
        hw_edges, hw_caveats = load_hw(
            hw_path, channel=args.hw_channel,
            threshold_v=args.threshold_v, hysteresis_v=args.hysteresis_v,
        )
        if args.tolerance_us < 0:
            raise ValueError("--tolerance-us must be >= 0")
        ref_edges = _shift(ref_edges, args.ref_offset_us)
        hw_edges = _shift(hw_edges, args.hw_offset_us)

        diff = refdiff_helper.reference_diff(
            ref_edges, hw_edges, args.tolerance_us
        )
        caveats = list(dict.fromkeys(ref_caveats + hw_caveats))
        rep = build_report(
            diff, ref=str(ref_path), hw=str(hw_path),
            tolerance_us=args.tolerance_us,
            ref_offset_us=args.ref_offset_us,
            hw_offset_us=args.hw_offset_us, caveats=caveats,
        )
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.md:
        md_path = Path(args.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_markdown(rep), encoding="utf-8")
        rep["markdown_report"] = str(md_path)
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
