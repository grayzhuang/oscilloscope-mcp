"""Find glitch runs — pulses shorter than a minimum width threshold.

A "glitch" in digital signals indicates setup/hold violations, runt pulses,
or ringing artefacts.  These are runs whose duration is shorter than the
expected minimum for the signal under analysis.

This is a pure function operating on the ``Run`` named-tuples produced by
:func:`oscilloscope_mcp.helpers.rle.runs_from_levels`.
"""

from __future__ import annotations

from typing import Sequence

from oscilloscope_mcp.helpers.rle import Run


def glitch_list(runs: Sequence[Run], min_width_us: float) -> list[dict]:
    """Return runs shorter than *min_width_us*.

    Parameters
    ----------
    runs : sequence of Run
        Time-ordered runs as produced by
        :func:`oscilloscope_mcp.helpers.rle.runs_from_levels`.
    min_width_us : float
        Minimum acceptable pulse width in microseconds.  Any run with
        ``dur_us < min_width_us`` is reported as a glitch.

    Returns
    -------
    list[dict]
        Each entry: ``{"t_us": float, "level": int, "dur_us": float,
        "index": int}`` where *index* is the position of the run in the
        input list.  Empty list when no glitches are found.
    """
    glitches: list[dict] = []
    for idx, run in enumerate(runs):
        if run.dur_us < min_width_us:
            glitches.append(
                {
                    "t_us": run.t_us,
                    "level": run.level,
                    "dur_us": run.dur_us,
                    "index": idx,
                }
            )
    return glitches
