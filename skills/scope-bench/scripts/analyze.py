"""analyze — offline event-stream analysis on captured scope data.

Bridges the analysis helpers that exist in ``oscilloscope_mcp.helpers``
but have no MCP tool entry point (glitch_list, edge_interval_stats,
pattern_search, causality_check, bus alignment). Operates on files, not
on the instrument, so it works with no scope and no MCP host — and on
captures far larger than the MCP response budget.

Accepted inputs (detected by shape, parsed leniently)
-----------------------------------------------------
* JSON saved from the ``scope_capture`` MCP tool — top-level
  ``waveform: {CHANn: {runs, edges}}`` (+ top-level ``caveats``).
* Hand-written JSON with top-level ``runs`` ``[[t_us, level, dur_us]]``
  and/or ``edges`` ``[[t_us, channel, kind] | [t_us, kind]]``.
* A ``scope_cli.py dump`` CSV (``t_s,volts``) plus its sibling
  ``<stem>.meta.json`` — re-quantized with ``--threshold-v`` /
  ``--hysteresis-v`` (Schmitt trigger, same convention as the tools).

Output is JSON on stdout (top-level ``schema`` version for forward
compatibility); ``--md`` switches to a markdown report instead.

Usage::

    python skills/scope-bench/scripts/analyze.py glitch  -i cap.json --min-width-us 0.05
    python skills/scope-bench/scripts/analyze.py jitter  -i cap.json --kind RISE
    python skills/scope-bench/scripts/analyze.py pattern -i cap.json --pattern 0,1,0,1
    python skills/scope-bench/scripts/analyze.py causality -i cap.json --a CHAN1 --b CHAN2 --max-delay-us 2
    python skills/scope-bench/scripts/analyze.py bus -i cap.json --channels CHAN1,CHAN2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from oscilloscope_mcp.helpers import bus as bus_helper
from oscilloscope_mcp.helpers import edges as edges_helper
from oscilloscope_mcp.helpers import quantize as quantize_helper
from oscilloscope_mcp.helpers import rle as rle_helper
from oscilloscope_mcp.helpers.causality_check import causality_check
from oscilloscope_mcp.helpers.edge_interval_stats import edge_interval_stats
from oscilloscope_mcp.helpers.glitch_list import glitch_list
from oscilloscope_mcp.helpers.pattern_search import pattern_search

SCHEMA_VERSION = "scope-analyze/1"


# ---------------------------------------------------------------------------
# Input loading (lenient)
# ---------------------------------------------------------------------------


def _runs_from_json(raw: list) -> list[rle_helper.Run]:
    return [
        rle_helper.Run(t_us=float(r[0]), level=int(r[1]), dur_us=float(r[2]))
        for r in raw
    ]


def _edges_from_json(raw: list, default_channel: str) -> list[edges_helper.Edge]:
    out: list[edges_helper.Edge] = []
    for e in raw:
        if len(e) >= 3:  # capture-style [t_us, channel, kind]
            out.append(
                edges_helper.Edge(
                    t_us=float(e[0]), channel=str(e[1]), kind=str(e[2])
                )
            )
        else:  # compare-style [t_us, kind]
            out.append(
                edges_helper.Edge(
                    t_us=float(e[0]), channel=default_channel, kind=str(e[1])
                )
            )
    return out


def _channel_data_from_json(
    body: dict[str, Any], default_channel: str
) -> dict[str, Any]:
    return {
        "runs": _runs_from_json(body.get("runs") or []),
        "edges": _edges_from_json(
            body.get("edges") or [], body.get("source", default_channel)
        ),
        "caveats": list(body.get("caveats") or []),
    }


def _load_csv_input(
    csv_path: Path, *, threshold_v: float, hysteresis_v: float
) -> dict[str, Any]:
    """Rebuild runs/edges from a dump CSV via Schmitt re-quantization."""
    meta_path = csv_path.with_name(csv_path.stem + ".meta.json")
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    volts: list[float] = []
    times: list[float] = []
    with csv_path.open("r", encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("t_s"):
                continue
            t_s, v = line.split(",")
            times.append(float(t_s))
            volts.append(float(v))
    if not volts:
        raise ValueError(f"no samples found in {csv_path}")

    t0_s = float(meta.get("t0_s", times[0]))
    dt_s = float(meta.get("dt_s", 0.0))
    if dt_s <= 0:
        if len(times) > 1:
            dt_s = times[1] - times[0]
        else:
            dt_s = 1.0

    source = str(meta.get("source", csv_path.stem))
    levels = quantize_helper.quantize(volts, threshold_v, hysteresis_v)
    runs = rle_helper.runs_from_levels(
        levels, t0_us=t0_s * 1e6, dt_us=dt_s * 1e6
    )
    edges = edges_helper.edges_from_runs(runs, source)
    caveats = list(meta.get("caveats") or [])
    caveats.append(
        f"CSV re-quantized offline: threshold_v={threshold_v}, "
        f"hysteresis_v={hysteresis_v} — edge times inherit the CSV's "
        f"{dt_s:.3g} s sample interval (one-sample quantization error)."
    )
    return {
        source: {"runs": runs, "edges": edges, "caveats": caveats}
    }


def load_input(
    path: Path,
    *,
    threshold_v: float,
    hysteresis_v: float,
) -> dict[str, dict[str, Any]]:
    """Load any accepted input into ``{channel: {runs, edges, caveats}}``."""
    if path.suffix.lower() == ".csv":
        return _load_csv_input(path, threshold_v=threshold_v,
                               hysteresis_v=hysteresis_v)

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at top level")

    # capture-style result: {"waveform": {CHANn: {...}}, "caveats": [...]}
    wf = data.get("waveform")
    if isinstance(wf, dict) and wf:
        top_caveats = list(data.get("caveats") or [])
        out = {}
        for ch, body in wf.items():
            ch_data = _channel_data_from_json(body, str(ch))
            ch_data["caveats"] = top_caveats + ch_data["caveats"]
            out[str(ch)] = ch_data
        return out

    # Hand-written single-channel: top-level runs and/or edges.
    if "runs" in data or "edges" in data:
        ch_data = _channel_data_from_json(data, "CHAN1")
        return {"CHAN1": ch_data}

    raise ValueError(
        f"{path}: no 'waveform', 'runs' or 'edges' found — expected a "
        "scope_capture result or a runs/edges document"
    )


def _pick_channel(
    channels: dict[str, dict[str, Any]], wanted: str | None
) -> tuple[str, dict[str, Any]]:
    if wanted is not None:
        if wanted not in channels:
            raise ValueError(
                f"channel {wanted!r} not in input (found: {sorted(channels)})"
            )
        return wanted, channels[wanted]
    first = sorted(channels)[0]
    return first, channels[first]


# ---------------------------------------------------------------------------
# Sub-command cores
# ---------------------------------------------------------------------------


def run_glitch(inp: dict, args: argparse.Namespace) -> dict[str, Any]:
    ch, data = _pick_channel(inp, args.channel)
    return {
        "command": "glitch",
        "channel": ch,
        "min_width_us": args.min_width_us,
        "glitches": glitch_list(data["runs"], args.min_width_us),
        "n_runs": len(data["runs"]),
        "caveats": data["caveats"],
    }


def run_jitter(inp: dict, args: argparse.Namespace) -> dict[str, Any]:
    ch, data = _pick_channel(inp, args.channel)
    kind = args.kind.upper()
    if kind not in ("RISE", "FALL"):
        raise ValueError(f"--kind must be RISE or FALL, got {args.kind!r}")
    return {
        "command": "jitter",
        "channel": ch,
        "kind": kind,
        "stats": edge_interval_stats(data["edges"], kind),
        "caveats": data["caveats"],
    }


def run_pattern(inp: dict, args: argparse.Namespace) -> dict[str, Any]:
    ch, data = _pick_channel(inp, args.channel)
    pattern = [int(p) for p in args.pattern.split(",")]
    if not pattern or any(p not in (0, 1) for p in pattern):
        raise ValueError(
            f"--pattern must be comma-separated 0/1 levels, got {args.pattern!r}"
        )
    return {
        "command": "pattern",
        "channel": ch,
        "pattern": pattern,
        "tolerance_us": args.tolerance_us,
        "matches": pattern_search(data["runs"], pattern, args.tolerance_us),
        "caveats": data["caveats"],
    }


def run_causality(inp: dict, args: argparse.Namespace) -> dict[str, Any]:
    a_ch, a_data = _pick_channel(inp, args.a)
    b_ch, b_data = _pick_channel(inp, args.b)
    result = causality_check(
        a_data["edges"], b_data["edges"],
        max_delay_us=args.max_delay_us,
        a_kind=args.a_kind.upper(), b_kind=args.b_kind.upper(),
    )
    return {
        "command": "causality",
        "a": a_ch,
        "b": b_ch,
        "a_kind": args.a_kind.upper(),
        "b_kind": args.b_kind.upper(),
        "max_delay_us": args.max_delay_us,
        **result,
        "caveats": a_data["caveats"] + b_data["caveats"],
    }


def run_bus(inp: dict, args: argparse.Namespace) -> dict[str, Any]:
    order = [c.strip() for c in args.channels.split(",") if c.strip()]
    missing = [c for c in order if c not in inp]
    if missing:
        raise ValueError(
            f"channels {missing} not in input (found: {sorted(inp)})"
        )
    bus_runs = bus_helper.bus_runs_from_channels([inp[c]["runs"] for c in order])
    return {
        "command": "bus",
        "channel_order_msb_first": order,
        "bus_runs": [list(br) for br in bus_runs],
        "caveats": sorted(
            {c for c in (inp[ch]["caveats"] for ch in order) for c in c}
        ),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def render_markdown(out: dict[str, Any]) -> str:
    cmd = out["command"]
    lines = [f"# scope-bench analyze — {cmd}", ""]
    if cmd == "glitch":
        lines.append(
            f"Channel `{out['channel']}`, minimum width "
            f"{out['min_width_us']} µs — "
            f"{len(out['glitches'])} glitch(es) in {out['n_runs']} runs."
        )
        if out["glitches"]:
            lines += ["", _table(
                ["t_us", "level", "dur_us", "run_index"],
                [[g["t_us"], g["level"], g["dur_us"], g["index"]]
                 for g in out["glitches"]],
            )]
    elif cmd == "jitter":
        s = out["stats"]
        lines.append(
            f"Channel `{out['channel']}`, {out['kind']}-edge interval "
            f"statistics ({s['count']} intervals):"
        )
        lines += ["", _table(
            ["count", "mean_us", "stddev_us", "min_us", "max_us", "p99_us"],
            [[s["count"], s["mean_us"], s["stddev_us"],
              s["min_us"], s["max_us"], s["p99_us"]]],
        )]
        if s["histogram"]:
            lines += ["", "Interval histogram (10 bins):", "",
                      _table(["lo_us", "hi_us", "count"],
                             [[b["lo_us"], b["hi_us"], b["count"]]
                              for b in s["histogram"]])]
    elif cmd == "pattern":
        lines.append(
            f"Channel `{out['channel']}`, pattern {out['pattern']} — "
            f"{len(out['matches'])} match(es)."
        )
        if out["matches"]:
            lines += ["", _table(
                ["t_us", "span_us", "run_index"],
                [[m["t_us"], m["dur_us"], m["index"]] for m in out["matches"]],
            )]
    elif cmd == "causality":
        lines.append(
            f"`{out['b']}` {out['b_kind']} must follow `{out['a']}` "
            f"{out['a_kind']} within {out['max_delay_us']} µs — "
            f"{out['matched']}/{out['total_a']} matched, "
            f"{out['violations']} violation(s)."
        )
        if out["violation_details"]:
            lines += ["", "Violations:", "",
                      _table(["a_t_us", "nearest_b_t_us", "delay_us"],
                             [[v["a_t_us"], v["nearest_b_t_us"], v["delay_us"]]
                              for v in out["violation_details"]])]
    elif cmd == "bus":
        lines.append(
            f"Bus channels MSB→LSB: {', '.join(out['channel_order_msb_first'])} "
            f"— {len(out['bus_runs'])} distinct segments."
        )
        if out["bus_runs"]:
            lines += ["", _table(
                ["t_us", "value", "dur_us"],
                [[r[0], f"0b{r[1]:0{len(out['channel_order_msb_first'])}b}", r[2]]
                 for r in out["bus_runs"]],
            )]
    if out.get("caveats"):
        lines += ["", "## Caveats", ""]
        lines += [f"- {c}" for c in out["caveats"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI assembly
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze",
        description=(
            "Offline analysis of captured scope data (glitch/jitter/pattern/"
            "causality/bus) — no instrument connection."
        ),
    )
    parser.add_argument(
        "-i", "--input", required=True,
        help="capture JSON, runs/edges JSON, or a dump CSV (+ .meta.json)",
    )
    parser.add_argument(
        "--threshold-v", type=float, default=1.5,
        help="CSV re-quantization threshold (V), default 1.5",
    )
    parser.add_argument(
        "--hysteresis-v", type=float, default=0.1,
        help="CSV re-quantization hysteresis band (V), default 0.1",
    )
    parser.add_argument(
        "--md", action="store_true", help="print a markdown report instead of JSON"
    )

    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("glitch", help="runs shorter than a minimum width")
    p.add_argument("--min-width-us", type=float, required=True,
                   help="minimum acceptable pulse width (µs)")
    p.add_argument("--channel", default=None, help="channel key (default: first)")

    p = sub.add_parser("jitter", help="interval stats between same-kind edges")
    p.add_argument("--kind", default="RISE", help="RISE or FALL (default RISE)")
    p.add_argument("--channel", default=None)

    p = sub.add_parser("pattern", help="find a 0/1 level sequence in the runs")
    p.add_argument("--pattern", required=True,
                   help="comma-separated levels, e.g. 0,1,0,1")
    p.add_argument("--tolerance-us", type=float, default=0.0,
                   help="duration tolerance vs first match (µs)")
    p.add_argument("--channel", default=None)

    p = sub.add_parser(
        "causality",
        help="check B follows A within a bounded delay (handshake/CDC)",
    )
    p.add_argument("--a", required=True, help="channel A key")
    p.add_argument("--b", required=True, help="channel B key")
    p.add_argument("--max-delay-us", type=float, required=True)
    p.add_argument("--a-kind", default="RISE")
    p.add_argument("--b-kind", default="RISE")

    p = sub.add_parser(
        "bus", help="time-align channels into a multi-bit bus value stream"
    )
    p.add_argument("--channels", required=True,
                   help="comma-separated, MSB first, e.g. CHAN4,CHAN3,CHAN2,CHAN1")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        inp = load_input(
            Path(args.input),
            threshold_v=args.threshold_v,
            hysteresis_v=args.hysteresis_v,
        )
        runner = {
            "glitch": run_glitch,
            "jitter": run_jitter,
            "pattern": run_pattern,
            "causality": run_causality,
            "bus": run_bus,
        }[args.command]
        out = runner(inp, args)
        out["schema"] = SCHEMA_VERSION
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.md:
        print(render_markdown(out), end="")
    else:
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
