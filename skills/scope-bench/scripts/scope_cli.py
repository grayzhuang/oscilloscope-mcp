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

Connection defaults come from ``SCOPE_MCP_HOST`` / ``SCOPE_MCP_PORT`` /
``SCOPE_MCP_MODEL``; output directory from ``--data-dir`` or
``SCOPE_MCP_DATA_DIR`` or ``./data``. Run inside the dedicated env::

    conda run -n oscScope-mcp python skills/scope-bench/scripts/scope_cli.py doctor --save
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from oscilloscope_mcp.helpers.caveat_calc import compute_caveats
from oscilloscope_mcp.instruments import ScopeDispatchError, open_scope
from oscilloscope_mcp.instruments._base import Scope, Waveform
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

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
