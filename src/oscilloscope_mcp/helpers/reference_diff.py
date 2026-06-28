"""Compare reference (simulator) edges against hardware (captured) edges.

This is the project's *raison d'etre* — P4 in the roadmap. Given a set of
edges from an RTL simulator (the golden reference) and a set of edges from
a real oscilloscope capture, produce a structured diff that pinpoints:

- **matched** edges (within tolerance — reported as "shifted" with delta)
- **missing** edges (in reference but not in hardware)
- **added** edges (in hardware but not in reference)
- **first_divergence_us** — the earliest time where ref and hw disagree

The diff is signal-agnostic: a SPI transaction, an I2C ack, a UART frame,
or a USB3300 ULPI bring-up all work the same way — they are just sequences
of RISE/FALL edges on a time axis.
"""

from __future__ import annotations

from typing import Sequence

from oscilloscope_mcp.helpers.edges import Edge, edges_from_runs
from oscilloscope_mcp.helpers.rle import Run


def runs_to_edges(runs: Sequence[Run], channel: str = "CH") -> list[Edge]:
    """Convert runs[] to edges[] -- thin wrapper around edges_from_runs().

    Convenience for callers who have runs (e.g. from a simulator output)
    and want to feed them into :func:`reference_diff`.
    """
    return edges_from_runs(list(runs), channel)


def reference_diff(
    ref_edges: list[Edge],
    hw_edges: list[Edge],
    tolerance_us: float,
) -> dict:
    """Compare reference (sim) edges against hardware (captured) edges.

    Algorithm:
    1. For each ref edge (in time order), find the nearest unmatched hw
       edge of the same kind (RISE/FALL) within tolerance_us. If found,
       mark both as consumed and record the pair as "shifted" (with delta).
    2. Unconsumed ref edges -> "missing" (sim expects it, HW doesn't have it).
    3. Unconsumed hw edges -> "added" (HW has it, sim doesn't expect it).
    4. ``first_divergence_us`` = earliest time among all missing + added
       edges (the first point where things go wrong). None if everything
       matched.

    Parameters
    ----------
    ref_edges : list[Edge]
        Edge list from the simulator (golden reference).
    hw_edges : list[Edge]
        Edge list from the hardware capture.
    tolerance_us : float
        Edges within this time distance (same kind) are considered a match.
        Must be >= 0.

    Returns
    -------
    dict
        {
            "matched": int,
            "shifted": [{ref_t_us, hw_t_us, delta_us, kind}, ...],
            "missing": [{t_us, kind}, ...],   # in ref but not in hw
            "added": [{t_us, kind}, ...],     # in hw but not in ref
            "first_divergence_us": float | None,
            "summary": str,
        }
    """
    if tolerance_us < 0:
        raise ValueError(f"tolerance_us must be >= 0, got {tolerance_us}")

    # Tiny epsilon to absorb IEEE-754 float noise on boundary comparisons.
    _EPS = tolerance_us * 1e-9 if tolerance_us > 0 else 0.0

    # Sort both lists by time for the greedy nearest-neighbor pass.
    sorted_ref = sorted(ref_edges, key=lambda e: e.t_us)
    sorted_hw = sorted(hw_edges, key=lambda e: e.t_us)

    # Track which hw edges have been consumed.
    hw_consumed: set[int] = set()  # indices into sorted_hw

    shifted: list[dict] = []
    missing: list[dict] = []

    # Greedy matching: for each ref edge, find closest unmatched hw edge
    # of the same kind within tolerance.
    for ref_edge in sorted_ref:
        best_idx: int | None = None
        best_dist: float = float("inf")

        for i, hw_edge in enumerate(sorted_hw):
            if i in hw_consumed:
                continue
            if hw_edge.kind != ref_edge.kind:
                continue
            dist = abs(hw_edge.t_us - ref_edge.t_us)
            if dist <= tolerance_us + _EPS and dist < best_dist:
                best_dist = dist
                best_idx = i

        if best_idx is not None:
            hw_edge = sorted_hw[best_idx]
            hw_consumed.add(best_idx)
            shifted.append({
                "ref_t_us": ref_edge.t_us,
                "hw_t_us": hw_edge.t_us,
                "delta_us": round(hw_edge.t_us - ref_edge.t_us, 6),
                "kind": ref_edge.kind,
            })
        else:
            missing.append({
                "t_us": ref_edge.t_us,
                "kind": ref_edge.kind,
            })

    # Unconsumed hw edges -> added.
    added: list[dict] = []
    for i, hw_edge in enumerate(sorted_hw):
        if i not in hw_consumed:
            added.append({
                "t_us": hw_edge.t_us,
                "kind": hw_edge.kind,
            })

    # First divergence: earliest time among missing + added.
    divergence_times: list[float] = []
    for m in missing:
        divergence_times.append(m["t_us"])
    for a in added:
        divergence_times.append(a["t_us"])

    first_divergence_us: float | None = (
        min(divergence_times) if divergence_times else None
    )

    matched = len(shifted)
    total_ref = len(sorted_ref)
    total_hw = len(sorted_hw)

    # Build summary.
    if not missing and not added:
        summary = (
            f"Perfect match: all {matched} edge(s) matched within "
            f"{tolerance_us} us tolerance."
        )
    else:
        parts: list[str] = []
        parts.append(f"{matched}/{total_ref} ref edges matched")
        if missing:
            parts.append(f"{len(missing)} missing")
        if added:
            parts.append(f"{len(added)} added")
        if first_divergence_us is not None:
            parts.append(f"first divergence at {first_divergence_us} us")
        summary = "; ".join(parts) + "."

    return {
        "matched": matched,
        "shifted": shifted,
        "missing": missing,
        "added": added,
        "first_divergence_us": first_divergence_us,
        "summary": summary,
    }
