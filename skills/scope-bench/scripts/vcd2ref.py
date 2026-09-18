"""vcd2ref — turn a simulator signal into a scope_compare reference.

The RTL-vs-hardware loop needs the golden edges in the same shape the
scope tools speak: ``[[t_us, "RISE"|"FALL"], …]``. This script produces
that from either side of the loop:

* **VCD** (Value Change Dump — what every RTL simulator emits): a
  minimal streaming parser, no pyvcd dependency. Single 1-bit scalar
  signal, selected by full hierarchical name or its last segment
  (``top.dut.clk`` ↔ ``--signal clk``); multi-bit vectors and x/z
  transitions are ignored.
* **CSV** in the ``scope_cli.py dump`` format (``t_s,volts``): for
  using one hardware capture as the reference for another (regression
  against a known-good board), quantized with the same Schmitt
  convention as the tools.

Output JSON (stdout or ``-o``): ``{"schema": "vcd2ref/1", "edges":
[[t_us, kind], …], …}`` — feed directly to ``scope_compare``'s
``reference_edges`` or to ``compare_rtl.py --ref``.

Usage::

    python skills/scope-bench/scripts/vcd2ref.py -i sim.vcd --signal top.uart.tx
    python skills/scope-bench/scripts/vcd2ref.py -i good_board.csv \\
        --threshold-v 1.65 --hysteresis-v 0.3 -o ref.json
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

SCHEMA_VERSION = "vcd2ref/1"

# VCD timescale unit → multiplier to microseconds.
_UNIT_TO_US = {
    "s": 1e6, "ms": 1e3, "us": 1.0, "µs": 1.0, "ns": 1e-3, "ps": 1e-6,
    "fs": 1e-9,
}


# ---------------------------------------------------------------------------
# VCD parsing (minimal, streaming, single 1-bit signal)
# ---------------------------------------------------------------------------


def _iter_vcd_changes(
    path: Path, target_id: str
) -> tuple[str, list[tuple[int, int]]]:
    """Stream the VCD once; return (timescale_text, [(t_vcd, level)]) for
    the target signal. Level changes only — the initial value (first 0/1
    seen) is not a transition. x/z changes are skipped."""
    timescale = ""
    changes: list[tuple[int, int]] = []
    level: int | None = None
    t_now = 0
    in_definitions = True

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if in_definitions:
                if line.startswith("$timescale"):
                    # "$timescale 1ns $end" or "$timescale 1 ns $end"
                    body = line.removeprefix("$timescale").removesuffix("$end")
                    timescale = body.strip()
                elif line.startswith("$enddefinitions"):
                    in_definitions = False
                continue  # $var/$scope/… are handled by the caller below

            if line.startswith("#"):
                t_now = int(line[1:])
            elif len(line) >= 2 and line[0] in "01xz":
                # Single-bit scalar change: value char glued to the id.
                value, vcd_id = line[0], line[1:]
                if vcd_id != target_id or value not in "01":
                    continue
                new_level = int(value)
                if level is None:
                    level = new_level  # initial value, not an edge
                elif new_level != level:
                    changes.append((t_now, new_level))
                    level = new_level
            # everything else ($dumpvars, $comment, b… vectors) skipped

    return timescale, changes


def _timescale_to_us(text: str) -> float:
    """'1ns' / '1 ns' / '10ps' → factor × time = µs."""
    parts = text.split()
    if len(parts) == 1:
        # value glued to unit ("1ns") — split at the first letter.
        head = parts[0]
        i = 0
        while i < len(head) and (head[i].isdigit() or head[i] in ".+-"):
            i += 1
        value, unit = head[:i], head[i:]
    elif len(parts) == 2:
        value, unit = parts
    else:
        raise ValueError(f"cannot parse $timescale {text!r}")
    if unit not in _UNIT_TO_US:
        raise ValueError(f"unknown timescale unit {unit!r} in {text!r}")
    return float(value) * _UNIT_TO_US[unit]


def vcd_to_edges(
    path: Path, signal: str
) -> tuple[list[list[Any]], dict[str, Any]]:
    """Parse one 1-bit signal out of a VCD into [[t_us, kind], …]."""
    ids, names = _scan_vcd_vars(path)
    # Match on full hierarchical name or its last segment; ambiguity is
    # an error (two candidates with the same short name).
    full_matches = [i for i, n in zip(ids, names) if n == signal]
    short_matches = [
        i for i, n in zip(ids, names) if n.split(".")[-1] == signal
    ]
    matches = full_matches or short_matches
    if not matches:
        raise ValueError(
            f"signal {signal!r} not found in {path.name}; 1-bit signals: "
            f"{sorted(set(names))[:40]}"
        )
    if len(set(matches)) > 1:
        dup = sorted({n for i, n in zip(ids, names) if i in matches})
        raise ValueError(
            f"signal {signal!r} is ambiguous in {path.name}: {dup}; "
            "use the full hierarchical name"
        )
    target_id = matches[0]
    full_name = next(n for i, n in zip(ids, names) if i == target_id)

    timescale, changes = _iter_vcd_changes(path, target_id)
    if not timescale:
        raise ValueError(f"no $timescale found in {path.name}")
    factor = _timescale_to_us(timescale)

    edges: list[list[Any]] = []
    for t_vcd, new_level in changes:
        kind = "RISE" if new_level == 1 else "FALL"
        edges.append([round(t_vcd * factor, 6), kind])
    info = {
        "source": full_name,
        "timescale": timescale,
        "n_changes": len(changes),
        "t_first_us": edges[0][0] if edges else None,
        "t_last_us": edges[-1][0] if edges else None,
    }
    return edges, info


def _scan_vcd_vars(path: Path) -> tuple[list[str], list[str]]:
    """Collect 1-bit scalar vars from the definitions section:
    (ids, full hierarchical names). Vector vars are skipped."""
    ids: list[str] = []
    names: list[str] = []
    scope: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("$enddefinitions"):
                break
            if line.startswith("$scope"):
                # "$scope module dut $end"
                parts = line.split()
                if len(parts) >= 3:
                    scope.append(parts[2])
            elif line.startswith("$upscope"):
                if scope:
                    scope.pop()
            elif line.startswith("$var"):
                # "$var wire 1 ! data [7:0] $end" — width is parts[2].
                parts = line.split()
                if len(parts) < 5 or parts[2] != "1":
                    continue  # vectors/real: not a 1-bit scalar
                ids.append(parts[3])
                names.append(".".join([*scope, parts[4]]))
    return ids, names


# ---------------------------------------------------------------------------
# CSV input (dump format) → edges
# ---------------------------------------------------------------------------


def csv_to_edges(
    path: Path, *, threshold_v: float, hysteresis_v: float
) -> tuple[list[list[Any]], dict[str, Any]]:
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

    meta_path = path.with_name(path.stem + ".meta.json")
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    t0 = float(meta.get("t0_s", times[0]))
    dt = float(meta.get("dt_s", 0.0)) or (
        (times[1] - times[0]) if len(times) > 1 else 1.0
    )

    levels = quantize_helper.quantize(volts, threshold_v, hysteresis_v)
    runs = rle_helper.runs_from_levels(levels, t0_us=t0 * 1e6, dt_us=dt * 1e6)
    source = str(meta.get("source", path.stem))
    edges = [
        [e.t_us, e.kind]
        for e in edges_helper.edges_from_runs(runs, source)
    ]
    info = {
        "source": source,
        "n_changes": len(edges),
        "t_first_us": edges[0][0] if edges else None,
        "t_last_us": edges[-1][0] if edges else None,
        "quantized": {"threshold_v": threshold_v,
                      "hysteresis_v": hysteresis_v},
    }
    return edges, info


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vcd2ref",
        description=(
            "Convert a simulator signal (VCD) or a dump CSV into the "
            "edges JSON that scope_compare / compare_rtl consume."
        ),
    )
    parser.add_argument("-i", "--input", required=True, help="VCD or dump CSV")
    parser.add_argument(
        "--signal", default=None,
        help="VCD signal name — full (top.dut.clk) or last segment (clk)",
    )
    parser.add_argument(
        "--threshold-v", type=float, default=1.5,
        help="CSV quantization threshold (V), default 1.5",
    )
    parser.add_argument(
        "--hysteresis-v", type=float, default=0.1,
        help="CSV quantization hysteresis band (V), default 0.1",
    )
    parser.add_argument(
        "--offset-us", type=float, default=0.0,
        help="shift the whole time axis by this many µs (align ref t=0 "
             "to the hardware trigger before diffing)",
    )
    parser.add_argument("-o", "--out", default=None,
                        help="write JSON here (default: stdout)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.input)
    try:
        if not path.is_file():
            raise ValueError(f"input file not found: {path}")
        if path.suffix.lower() == ".vcd":
            if not args.signal:
                raise ValueError("--signal is required for VCD input")
            edges, info = vcd_to_edges(path, args.signal)
            input_kind = "vcd"
        elif path.suffix.lower() == ".csv":
            edges, info = csv_to_edges(
                path, threshold_v=args.threshold_v,
                hysteresis_v=args.hysteresis_v,
            )
            input_kind = "csv"
        else:
            raise ValueError(
                f"unsupported input {path.name!r} — expected .vcd or .csv"
            )
        if args.offset_us:
            edges = [[round(t + args.offset_us, 6), k] for t, k in edges]
            info["offset_us"] = args.offset_us

        out = {
            "schema": SCHEMA_VERSION,
            "input_kind": input_kind,
            **info,
            "edges": edges,
        }
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    text = json.dumps(out, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"written": str(out_path), "n_edges": len(edges)}))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
