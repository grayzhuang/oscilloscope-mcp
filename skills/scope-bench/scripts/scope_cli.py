"""scope_cli — online bridge from a shell to the oscilloscope_mcp library.

Covers what the MCP tools cannot do from inside a host: writing raw
voltage arrays to disk (the MCP response budget is ~10 KB), and driving
the scope without any MCP host at all. Every sub-command goes through
``open_scope`` + the ``Scope`` driver — the same validation path as the
MCP tools; no SCPI is hand-assembled here, and model-specific behaviour
(RAW-requires-STOP, port defaults, image formats) is enforced by the
driver layer, not by this script.

Sub-commands
------------
doctor   Read-only health check: env → *IDN? → profile capability summary
         → per-channel / timebase / acquire / trigger snapshot, with
         observation-limit caveats. ``--save`` additionally writes
         ``data/<model>_setup_<ts>.yaml``.
dump     Read raw per-channel voltages into
         ``data/<model>_waveform_<ts>_<ch>.csv`` (columns ``t_s,volts``)
         plus a sibling ``.meta.json`` (t0/dt/n/scale/offset/caveats).
         ``--mode RAW`` reads full acquisition memory; the driver raises
         if the scope is not stopped first.
meas-log Poll ``scope.measure`` on a fixed interval and append each
         sample to ``data/<model>_measlog_<ts>.csv``
         (``elapsed_s,<item>,…``), then print per-item min/max/avg/σ.
         Ctrl+C ends the run gracefully and still prints the summary.
         Feed the CSV to ``plot.py trend`` for a PNG.
screenshot Save the current screen to ``data/<model>_screenshot_<ts>.<ext>``
         — the extension follows the format the instrument actually
         returned (``image_format``), not the request.

Connection defaults come from ``SCOPE_MCP_HOST`` / ``SCOPE_MCP_PORT`` /
``SCOPE_MCP_MODEL``; output directory from ``--data-dir`` or
``SCOPE_MCP_DATA_DIR`` or ``./data``. Run inside the dedicated env::

    conda run -n oscScope-mcp python skills/scope-bench/scripts/scope_cli.py doctor --save
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from oscilloscope_mcp.helpers import measure as meas_helper
from oscilloscope_mcp.helpers.caveat_calc import (
    caveat_for_unmeasurable_items,
    compute_caveats,
)
from oscilloscope_mcp.instruments import ScopeDispatchError, open_scope
from oscilloscope_mcp.instruments._base import Scope, ScreenshotPlan, Waveform
from oscilloscope_mcp.transport.scpi_lan import ScpiError

SCHEMA_VERSION = "scope-cli/1"
DUMP_META_SCHEMA = "scope-dump/1"

# profile.capability keys surfaced in the doctor summary (the facts an
# agent needs when planning a capture: BW, channel count, sample-rate
# derating, memory, screenshot formats). PyYAML parses profile values
# like "100.0e6" as strings (its float resolver needs a signed exponent),
# so scalar keys listed here are normalized to float when possible.
_CAP_KEYS = (
    "analog_bw_hz",
    "channels",
    "sample_rate_mode_dependent",
    "memory_depth_pts_total",
    "screenshot_formats",
)
_CAP_FLOAT_KEYS = ("analog_bw_hz", "memory_depth_pts_total")


def _capability_summary(cap: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _CAP_KEYS:
        if key not in cap:
            continue
        val = cap[key]
        if key in _CAP_FLOAT_KEYS:
            try:
                val = float(val)
            except (TypeError, ValueError):
                pass
        out[key] = val
    return out


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _model_stem(scope: Scope) -> str:
    """Registry model name → filesystem stem (``ZLG_ZDS1104`` → ``zds1104``).

    Matches the existing artifacts in ``data/`` (e.g.
    ``zds1104_setup_20260917.yaml``): vendor prefix dropped, lower-case.
    """
    model = str(scope.profile.get("model") or "scope")
    return model.lower().split("_")[-1]


def _resolve_data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("SCOPE_MCP_DATA_DIR")
    return Path(env) if env else Path("data")


def _channel_meta(scope: Scope, source: str) -> dict[str, float | None]:
    """Best-effort vertical context (scale / offset) for a dumped channel.

    ``channel_scale_v_per_div`` / ``channel_offset_v`` are driver
    conveniences (not part of the Scope ABC), so absence on a future
    driver just yields nulls rather than failing the dump.
    """
    out: dict[str, float | None] = {}
    digits = "".join(c for c in source if c.isdigit())
    ch_n = int(digits) if digits else 0
    for key, getter in (
        ("scale_v_per_div", "channel_scale_v_per_div"),
        ("offset_v", "channel_offset_v"),
    ):
        fn = getattr(scope, getter, None)
        if callable(fn) and ch_n:
            try:
                out[key] = float(fn(ch_n))
            except Exception:
                out[key] = None
        else:
            out[key] = None
    return out


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def run_doctor(
    scope: Scope, *, host: str, save: bool, data_dir: Path
) -> dict[str, Any]:
    """Read-only snapshot; ``save`` mirrors it to a YAML in ``data_dir``."""
    cap = dict(scope.profile.get("capability") or {})
    n_channels = int(cap.get("channels") or 4)

    channels: dict[str, Any] = {}
    active = 0
    for n in range(1, n_channels + 1):
        st = scope.get_channel(n)
        channels[f"CH{n}"] = asdict(st)
        if st.display:
            active += 1

    timebase = scope.get_timebase()
    acquire = scope.get_acquire()
    trigger = scope.get_trigger()

    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "model": scope.profile.get("model"),
        "host": host,
        "idn": scope.idn(),
        "run_state": trigger.status,
        "capability": _capability_summary(cap),
        "channels": channels,
        "timebase": asdict(timebase),
        "acquire": asdict(acquire),
        "trigger": {
            "mode": trigger.mode,
            "status": trigger.status,
            "sweep": trigger.sweep,
            "coupling": trigger.coupling,
            "holdoff_s": trigger.holdoff_s,
            "params": trigger.params,
        },
        "caveats": compute_caveats(
            scope.profile,
            active_channel_count=active,
            timebase_s_per_div=timebase.s_per_div,
        ),
    }

    if save:
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / f"{_model_stem(scope)}_setup_{_timestamp()}.yaml"
        path.write_text(
            yaml.safe_dump(result, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        result["saved_to"] = str(path)
    return result


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------


def _write_samples_csv(path: Path, wf: Waveform) -> None:
    """Write ``t_s,volts`` rows; 9/6 significant digits keep the file
    round-trippable without float-noise bloat."""
    t0, dt = wf.t0_s, wf.dt_s
    with path.open("w", encoding="ascii", newline="\n") as f:
        f.write("t_s,volts\n")
        chunk: list[str] = []
        for i, v in enumerate(wf.volts):
            chunk.append(f"{t0 + i * dt:.9g},{v:.6g}\n")
            if len(chunk) >= 65536:
                f.write("".join(chunk))
                chunk.clear()
        if chunk:
            f.write("".join(chunk))


def run_dump(
    scope: Scope,
    *,
    channels: list[str],
    mode: str,
    host: str,
    data_dir: Path,
) -> dict[str, Any]:
    """Dump raw volts per channel to CSV + meta. RAW-mode STOP enforcement
    lives in the driver — a running scope raises before any file is
    written for that channel."""
    ts = _timestamp()
    stem = _model_stem(scope)
    active = scope.active_channel_count()
    tb_s = scope.timebase_s_per_div()
    caveats = compute_caveats(
        scope.profile,
        active_channel_count=active,
        timebase_s_per_div=tb_s,
    )

    data_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for ch in channels:
        wf = scope.read_waveform(ch, mode=mode)
        csv_path = data_dir / f"{stem}_waveform_{ts}_{wf.source.lower()}.csv"
        meta_path = csv_path.with_name(csv_path.stem + ".meta.json")
        _write_samples_csv(csv_path, wf)
        meta: dict[str, Any] = {
            "schema": DUMP_META_SCHEMA,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "model": scope.profile.get("model"),
            "host": host,
            "source": wf.source,
            "mode": mode.upper(),
            "n_samples": wf.n_samples,
            "t0_s": wf.t0_s,
            "dt_s": wf.dt_s,
            "csv": csv_path.name,
            "caveats": caveats,
        }
        meta.update(_channel_meta(scope, wf.source))
        meta_path.write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        files.append(
            {
                "channel": wf.source,
                "csv": str(csv_path),
                "meta": str(meta_path),
                "n_samples": wf.n_samples,
            }
        )

    return {
        "schema": SCHEMA_VERSION,
        "mode": mode.upper(),
        "files": files,
        "caveats": caveats,
    }


# ---------------------------------------------------------------------------
# meas-log
# ---------------------------------------------------------------------------


def run_meas_log(
    scope: Scope,
    *,
    items: list[str],
    source: str,
    interval_s: float,
    duration_s: float,
    host: str,
    data_dir: Path,
) -> dict[str, Any]:
    """Poll ``scope.measure`` on a fixed cadence into one CSV.

    Timing anchors on the loop start (not the previous sample), so slow
    queries shift the phase but don't drift the schedule. A query value
    the scope can't measure lands as an empty cell (→ None downstream),
    keeping row/column alignment for ``plot.py trend``.
    """
    if interval_s <= 0:
        raise ValueError(f"--interval must be > 0, got {interval_s}")
    if duration_s <= 0:
        raise ValueError(f"--duration must be > 0, got {duration_s}")
    # Validate items + source before the first instrument read.
    resolved = meas_helper.resolve_items(scope.profile, items)
    norm_source = meas_helper.normalize_source(scope.profile, source)

    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / f"{_model_stem(scope)}_measlog_{_timestamp()}.csv"
    start = time.monotonic()
    rows: list[list[float | None]] = []  # [elapsed, item1, item2, …]

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration_s:
                break
            meas = scope.measure(items, source=norm_source)
            row: list[float | None] = [elapsed]
            row.extend(meas.get(name) for name in items)
            rows.append(row)
            next_at = (len(rows)) * interval_s
            remain = next_at - (time.monotonic() - start)
            if remain > 0:
                time.sleep(remain)
    except KeyboardInterrupt:
        pass  # Ctrl+C: end early, still produce the summary below

    with csv_path.open("w", encoding="ascii", newline="\n") as f:
        f.write("elapsed_s," + ",".join(items) + "\n")
        for row in rows:
            cells = [f"{row[0]:.6g}"] + [
                "" if v is None else f"{v:.9g}" for v in row[1:]
            ]
            f.write(",".join(cells) + "\n")

    stats: dict[str, Any] = {}
    unmeasurable_last: list[str] = []
    for idx, name in enumerate(items):
        vals = [r[idx + 1] for r in rows if r[idx + 1] is not None]
        if vals:
            stats[name] = {
                "n": len(vals),
                "min": min(vals),
                "max": max(vals),
                "avg": statistics.fmean(vals),
                "stddev": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            }
        else:
            stats[name] = None
            unmeasurable_last.append(name)

    caveats = compute_caveats(
        scope.profile,
        active_channel_count=scope.active_channel_count(),
        timebase_s_per_div=scope.timebase_s_per_div(),
    )
    caveats.extend(caveat_for_unmeasurable_items(unmeasurable_last))
    return {
        "schema": SCHEMA_VERSION,
        "source": norm_source,
        "items": items,
        "csv": str(csv_path),
        "samples": len(rows),
        "interval_s": interval_s,
        "duration_s": duration_s,
        "stats": stats,
        "caveats": caveats,
    }


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


def run_screenshot(
    scope: Scope, *, host: str, data_dir: Path
) -> dict[str, Any]:
    """Dump the screen un-annotated; the extension follows the format
    the instrument actually returned (some families ignore the request
    and always emit one format)."""
    result = scope.screenshot(ScreenshotPlan())
    ext = (result.image_format or "PNG").strip().lower()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{_model_stem(scope)}_screenshot_{_timestamp()}.{ext}"
    path.write_bytes(result.image_bytes)
    return {
        "schema": SCHEMA_VERSION,
        "path": str(path),
        "bytes_written": len(result.image_bytes),
        "image_format": result.image_format,
        "caveats": [],
    }


# ---------------------------------------------------------------------------
# CLI assembly
# ---------------------------------------------------------------------------


def _common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--host", default=None,
        help="scope host/IP (default: SCOPE_MCP_HOST)",
    )
    p.add_argument(
        "--port", type=int, default=None,
        help="SCPI port (default: SCOPE_MCP_PORT, else profile default)",
    )
    p.add_argument(
        "--model", default=None,
        help="registry model, e.g. RIGOL_DS1104Z (default: SCOPE_MCP_MODEL, "
             "else *IDN? auto-detect)",
    )
    p.add_argument("--timeout", type=float, default=10.0, help="SCPI timeout (s)")
    p.add_argument(
        "--data-dir", default=None,
        help="output directory (default: SCOPE_MCP_DATA_DIR, else ./data)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scope_cli",
        description=(
            "Online CLI for the oscilloscope_mcp library — doctor checks and "
            "raw-voltage dumps that exceed the MCP response budget."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser(
        "doctor",
        help="read-only health check + setup snapshot (use --save to persist)",
        description=(
            "Env check → *IDN? → profile capability summary → channel/"
            "timebase/acquire/trigger snapshot with caveats. Never writes "
            "to the instrument."
        ),
    )
    _common_args(p_doctor)
    p_doctor.add_argument(
        "--save", action="store_true",
        help="also write data/<model>_setup_<ts>.yaml",
    )

    p_dump = sub.add_parser(
        "dump",
        help="dump raw per-channel voltages to CSV + .meta.json",
        description=(
            "Reads each channel's waveform and writes "
            "data/<model>_waveform_<ts>_<ch>.csv (t_s,volts) plus a sibling "
            ".meta.json. --mode RAW reads full acquisition memory and "
            "requires the scope to be stopped (enforced by the driver)."
        ),
    )
    _common_args(p_dump)
    p_dump.add_argument(
        "channels", nargs="+", metavar="CHAN",
        help="channel(s) to dump, e.g. CHAN1 CHAN2",
    )
    p_dump.add_argument(
        "--mode", default="NORMal",
        help="waveform read mode: NORMal or RAW (default NORMal)",
    )

    p_meas = sub.add_parser(
        "meas-log",
        help="poll measurements on an interval → CSV + per-item statistics",
        description=(
            "Repeatedly reads scope-side measurements and appends them to "
            "data/<model>_measlog_<ts>.csv (elapsed_s,item1,item2,…), then "
            "prints min/max/avg/stddev per item. Ctrl+C ends the run early "
            "and still prints the summary. The CSV feeds plot.py trend."
        ),
    )
    _common_args(p_meas)
    p_meas.add_argument(
        "--items", required=True,
        help="comma-separated canonical items, e.g. VPP,FREQUENCY",
    )
    p_meas.add_argument("--source", default="CHAN1", help="source channel")
    p_meas.add_argument(
        "--interval", type=float, default=1.0, help="poll interval (s)"
    )
    p_meas.add_argument(
        "--duration", type=float, default=10.0,
        help="total logging duration (s)",
    )

    p_shot = sub.add_parser(
        "screenshot",
        help="save the current screen (extension follows image_format)",
        description=(
            "Saves the current screen image to "
            "data/<model>_screenshot_<ts>.<ext>. The extension follows the "
            "format the instrument returned in its response, not the "
            "request. For annotated screenshots (cursors, channel labels) "
            "use the MCP scope_screenshot tool instead."
        ),
    )
    _common_args(p_shot)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode_upper = getattr(args, "mode", "NORMAL").strip().upper()
    if mode_upper not in ("NORMAL", "RAW"):
        print(
            f"error: --mode must be 'NORMal' or 'RAW', got {args.mode!r}",
            file=sys.stderr,
        )
        return 2

    try:
        scope = open_scope(
            host=args.host, port=args.port, model=args.model,
            timeout_s=args.timeout,
        )
        host_used = args.host or os.environ.get("SCOPE_MCP_HOST", "")
        data_dir = _resolve_data_dir(args.data_dir)
        if args.command == "doctor":
            out = run_doctor(
                scope, host=host_used, save=args.save, data_dir=data_dir
            )
        elif args.command == "meas-log":
            out = run_meas_log(
                scope,
                items=[i.strip() for i in args.items.split(",") if i.strip()],
                source=args.source,
                interval_s=args.interval,
                duration_s=args.duration,
                host=host_used,
                data_dir=data_dir,
            )
        elif args.command == "screenshot":
            out = run_screenshot(scope, host=host_used, data_dir=data_dir)
        else:
            out = run_dump(
                scope,
                channels=[c.strip() for c in args.channels],
                mode=mode_upper,
                host=host_used,
                data_dir=data_dir,
            )
    except ScopeDispatchError as e:
        print(f"error: scope dispatch failed: {e}", file=sys.stderr)
        return 1
    except ScpiError as e:
        print(f"error: SCPI transport error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        # Driver-enforced preconditions, e.g. RAW dump while still running.
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        # Profile validation (bad item name / channel / param) — the
        # validation helpers list the legal values in the message.
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
