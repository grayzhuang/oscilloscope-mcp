"""Cross-channel causality check: "B follows A within N us of every A edge".

Used to verify handshake protocols, CDC acknowledgements, and any
cross-channel timing obligation where channel B must respond to channel A
within a bounded delay.
"""

from __future__ import annotations

from typing import Sequence

from oscilloscope_mcp.helpers.edges import Edge


def causality_check(
    edges_a: Sequence[Edge],
    edges_b: Sequence[Edge],
    max_delay_us: float,
    a_kind: str = "RISE",
    b_kind: str = "RISE",
) -> dict:
    """Check that every a_kind edge on A is followed by a b_kind edge on B
    within max_delay_us.

    Parameters
    ----------
    edges_a : sequence of Edge
        Edges from channel A (from edges.py).
    edges_b : sequence of Edge
        Edges from channel B.
    max_delay_us : float
        Maximum allowed delay in microseconds between an A edge and the
        corresponding B edge.
    a_kind : str
        Which edge type on A to match ("RISE" or "FALL").
    b_kind : str
        Which edge type on B should follow ("RISE" or "FALL").

    Returns
    -------
    dict
        {"total_a": int, "matched": int, "violations": int,
         "violation_details": [{"a_t_us": float, "nearest_b_t_us": float|None,
                                "delay_us": float|None}, ...]}

        A violation = no matching B edge within max_delay_us after an A edge.
    """
    # Filter edges by kind
    a_times = [e.t_us for e in edges_a if e.kind == a_kind]
    b_times = [e.t_us for e in edges_b if e.kind == b_kind]

    total_a = len(a_times)
    matched = 0
    violations: list[dict] = []

    for a_t in a_times:
        # Find the first B edge that occurs at or after a_t
        best_b: float | None = None
        best_delay: float | None = None

        for b_t in b_times:
            if b_t >= a_t:
                delay = b_t - a_t
                if best_b is None or delay < best_delay:  # type: ignore[operator]
                    best_b = b_t
                    best_delay = delay
                break  # b_times assumed in order, first >= a_t is nearest

        if best_b is not None and best_delay is not None and best_delay <= max_delay_us:
            matched += 1
        else:
            violations.append({
                "a_t_us": a_t,
                "nearest_b_t_us": best_b,
                "delay_us": best_delay,
            })

    return {
        "total_a": total_a,
        "matched": matched,
        "violations": len(violations),
        "violation_details": violations,
    }
