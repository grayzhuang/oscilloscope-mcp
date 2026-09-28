"""Latency boundary bench for the near-realtime web-viewer use case.

Measures, against a live scope (ZDS1104 verified; RIGOL branch included
but not yet run against hardware), using THIS project's transport path:
  1. raw TCP connect time (fresh connection, xN)
  2. *IDN? RTT via ScpiLan (connection-per-call, the current library path)
  3. library read_waveform() frame latency (connect + I/O + parse)
  4. long-lived-connection frame loop (single socket, back-to-back
     waveform queries) -> the instrument/network latency floor
  5. host-side serialization cost for the captured samples

Memory-depth sweep: frames get smaller as MDEP drops, so the bench
walks the ladder and RESTORES the original MDEP at exit (restored via
raw SCPI write because the original value may sit outside the profile's
legal ladder, e.g. an AUTO-coupled 3.5M).

Read-only apart from the MDEP sweep; run state is never touched.
Results & conclusions: docs/web-ui-latency-bench.md.

Usage:
  SCOPE_MCP_HOST=<ip> SCOPE_MCP_MODEL=ZLG_ZDS1104 \
    conda run -n oscScope-mcp python scripts/bench_latency.py
  SCOPE_MCP_HOST=<ip> SCOPE_MCP_MODEL=RIGOL_DS1104Z \
    conda run -n oscScope-mcp python scripts/bench_latency.py --port 5555
"""

from __future__ import annotations

import argparse
import array
import json
import socket
import statistics
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oscilloscope_mcp.instruments import open_scope  # noqa: E402
from oscilloscope_mcp.instruments._base import AcquireSetup  # noqa: E402
from oscilloscope_mcp.transport.scpi_lan import ScpiLan, _recv_exactly  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def stats(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    s = sorted(samples)
    n = len(s)
    return {
        "n": n,
        "min_ms": round(s[0] * 1000, 2),
        "p50_ms": round(statistics.median(s) * 1000, 2),
        "p90_ms": round(s[min(n - 1, int(n * 0.9))] * 1000, 2),
        "max_ms": round(s[-1] * 1000, 2),
    }


def bench_connect(host: str, port: int, n: int = 5) -> list[float]:
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        s = socket.create_connection((host, port), timeout=5)
        dt = time.perf_counter() - t0
        s.close()
        out.append(dt)
    return out


def bench_idn(tr: ScpiLan, n: int = 10) -> tuple[list[float], str]:
    rtts, last = [], ""
    for _ in range(n):
        t0 = time.perf_counter()
        last = tr.query("*IDN?")
        rtts.append(time.perf_counter() - t0)
    return rtts, last


def bench_lib_waveform(scope, n: int = 3) -> tuple[list[float], int]:
    rtts, pts = [], 0
    for _ in range(n):
        t0 = time.perf_counter()
        wf = scope.read_waveform("CHAN1", "NORMal")
        rtts.append(time.perf_counter() - t0)
        pts = wf.n_samples
    return rtts, pts


def bench_longconn_zds(host: str, port: int, frames: int) -> dict:
    """Single-socket back-to-back :GLOBal:MULTiwave? SCREen loop (ZDS framing).

    Trailer handling: the first frame is followed by a best-effort probe
    with a 0.6 s window (ZDS response latency reaches 0.42 s), which is
    long enough to observe the trailing CRLF exactly once. Later frames
    then recv_exactly(len(tail)) with no extra wait, keeping the loop
    truly back-to-back.
    """
    cmd = b":GLOBal:MULTiwave? SCREen,CHANnel1\n"
    rtts, sizes = [], []
    empty_retries = 0
    with socket.create_connection((host, port), timeout=15) as sock:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        done = 0
        tail = None
        while done < frames:
            t0 = time.perf_counter()
            sock.sendall(cmd)
            (n,) = struct.unpack("<i", _recv_exactly(sock, 4))
            if n <= 0:
                empty_retries += 1
                if empty_retries > 12:
                    raise RuntimeError("MULTiwave keeps returning empty blocks")
                time.sleep(1.0)
                continue
            payload = _recv_exactly(sock, n)
            if tail is None:
                sock.settimeout(0.6)
                try:
                    tail = sock.recv(64)
                except OSError:
                    tail = b""
            elif tail:
                _recv_exactly(sock, len(tail))
            dt = time.perf_counter() - t0
            rtts.append(dt)
            sizes.append(n)
            done += 1
    return {"rtts": rtts, "payload_bytes": sizes, "tail": tail or b"", "empty_retries": empty_retries}


def bench_serialize(volts: list[float]) -> dict:
    out = {}
    t0 = time.perf_counter()
    for _ in range(5):
        json.dumps({"v": volts})
    out["json_5x_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    t0 = time.perf_counter()
    for _ in range(5):
        array.array("f", volts).tobytes()
    out["bin32_5x_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.138.14")
    ap.add_argument("--port", type=int, default=5025)
    ap.add_argument("--model", default="ZLG_ZDS1104")
    ap.add_argument("--frames-per-mdep", type=int, default=15)
    args = ap.parse_args()

    report: dict = {"host": args.host, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}

    print(f"[1] TCP connect x5 to {args.host}:{args.port}...")
    report["connect"] = stats(bench_connect(args.host, args.port))
    print("   ", report["connect"])

    tr = ScpiLan(host=args.host, port=args.port, timeout_s=10)
    print("[2] *IDN? x10 via library (connection-per-call)...")
    rtts, idn = bench_idn(tr)
    report["idn_perconn"] = stats(rtts)
    report["idn"] = idn
    print("   ", report["idn_perconn"], "|", idn)

    print("[3] open_scope + read-only state...")
    scope = open_scope(host=args.host, port=args.port, model=args.model, timeout_s=15)
    acq0 = scope.get_acquire()
    orig_mdep = getattr(acq0, "memory_depth", None)
    report["acquire_original"] = {
        "mem_depth": orig_mdep,
        "sample_rate": getattr(acq0, "sample_rate", None),
    }
    report["run_state"] = scope.query_raw(":GLOBal:RUN:STATe?")
    print("   ", report["acquire_original"], "| run:", report["run_state"])

    mdep_pts = _parse_mdep(orig_mdep) if isinstance(orig_mdep, str) else (orig_mdep or 0)
    # current value first (kept as-is when it is an AUTO-coupled value
    # outside the legal ladder), then walk down the ladder.
    ladder: list[tuple[str | None, int]] = [
        (str(orig_mdep) if orig_mdep is not None else None, min(3, args.frames_per_mdep)),
        ("700000", args.frames_per_mdep),
        ("140000", args.frames_per_mdep),
        ("14000", args.frames_per_mdep),
    ]

    print("[4] MDEP sweep (long-connection loop + library x3 per step)...")
    sweep = []
    for mdep, frames in ladder:
        if mdep is not None and _in_ladder(mdep):
            scope.set_acquire(AcquireSetup(memory_depth=int(mdep)))
        else:
            print(f"    step mdep={mdep}: kept as-is (not in profile ladder)")
        time.sleep(1.0)
        bench_lib_waveform(scope, n=1)  # warm up: absorb post-config empty blocks
        acq = scope.get_acquire()
        pts = _parse_mdep(getattr(acq, "memory_depth", "")) or 0
        if pts and pts > 500_000:
            frames = min(frames, 5)
        lc = bench_longconn_zds(args.host, args.port, frames)
        lib_rtts, lib_pts = bench_lib_waveform(scope, n=3)
        step = {
            "mdep_set": mdep,
            "mdep_read": getattr(acq, "memory_depth", None),
            "sample_rate": getattr(acq, "sample_rate", None),
            "payload_bytes": lc["payload_bytes"][0] if lc["payload_bytes"] else None,
            "longconn": stats(lc["rtts"]),
            "lib_x3": stats(lib_rtts),
            "tail": lc["tail"].decode("ascii", "replace"),
        }
        step["fps_longconn_p50"] = round(1000 / step["longconn"]["p50_ms"], 2) if step["longconn"]["p50_ms"] else None
        sweep.append(step)
        print(f"    mdep={step['mdep_read']} sr={step['sample_rate']} bytes={step['payload_bytes']} "
              f"longconn p50={step['longconn']['p50_ms']}ms fps~{step['fps_longconn_p50']} | "
              f"lib p50={step['lib_x3']['p50_ms']}ms")
    report["sweep"] = sweep

    print("[5] host-side serialization cost...")
    wf = scope.read_waveform("CHAN1", "NORMal")
    report["serialize"] = bench_serialize(wf.volts)
    report["serialize"]["n_points"] = wf.n_samples
    print("   ", report["serialize"])

    print("[6] restore original MDEP...")
    if orig_mdep is not None:
        if _in_ladder(orig_mdep):
            scope.transport.write(f":ACQuire:MDEPth {orig_mdep}")
        else:
            # original value (e.g. an AUTO-coupled 3.5M) is not directly
            # settable — AUTO reproduces it given the untouched timebase
            scope.transport.write(":ACQuire:MDEPth AUTO")
        time.sleep(1.0)
        acq_r = scope.get_acquire()
        report["acquire_restored"] = {
            "mem_depth": getattr(acq_r, "memory_depth", None),
            "sample_rate": getattr(acq_r, "sample_rate", None),
        }
        print("   ", report["acquire_restored"])

    out = Path(__file__).with_name("tmp_bench_latency_result.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("full report ->", out)
    return 0


_ZDS_LADDER = {1400, 14000, 140000, 700000, 1400000, 7000000, 14000000, 28000000}


def _in_ladder(v) -> bool:
    return _parse_mdep(str(v)) in _ZDS_LADDER


def _parse_mdep(s) -> int:
    s = str(s).strip().upper()
    if s in ("", "NONE", "AUTO"):
        return 0
    mult = {"K": 1_000, "M": 1_000_000, "G": 1_000_000_000}
    if s[-1] in mult:
        try:
            return int(float(s[:-1]) * mult[s[-1]])
        except ValueError:
            return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
