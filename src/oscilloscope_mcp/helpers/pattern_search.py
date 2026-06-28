"""Search for a bit-pattern in the RLE run stream (template matching).

Finds occurrences of a level sequence (e.g. ``[0, 1, 0, 1]``) in the run
stream.  Supports optional tolerance-based duration matching: when
``tolerance_us > 0``, the first match becomes the reference template and
subsequent matches must have each run's duration within *tolerance_us* of
the corresponding reference run's duration.

This is a pure function operating on the ``Run`` named-tuples produced by
:func:`oscilloscope_mcp.helpers.rle.runs_from_levels`.
"""

from __future__ import annotations

from typing import Sequence

from oscilloscope_mcp.helpers.rle import Run


def pattern_search(
    runs: Sequence[Run],
    pattern: list[int],
    tolerance_us: float = 0.0,
) -> list[dict]:
    """Find occurrences of *pattern* (list of 0/1 levels) in the run stream.

    Parameters
    ----------
    runs : sequence of Run
        Time-ordered runs as produced by
        :func:`oscilloscope_mcp.helpers.rle.runs_from_levels`.
    pattern : list[int]
        Sequence of expected levels (0/1).  Length determines how many
        consecutive runs must match.
    tolerance_us : float
        When > 0, enables relative template matching.  The first match
        becomes the duration template; subsequent candidate matches must
        have each run's ``dur_us`` within *tolerance_us* of the
        corresponding template run's ``dur_us``.

    Returns
    -------
    list[dict]
        Each match: ``{"index": int, "t_us": float, "dur_us": float}``
        where *index* is the starting run index, *t_us* is the start time
        of the first run in the match, and *dur_us* is the total span of
        all matched runs.  Empty list when no matches are found.
    """
    if not pattern or not runs:
        return []

    pat_len = len(pattern)
    n = len(runs)
    if pat_len > n:
        return []

    matches: list[dict] = []
    template_durations: list[float] | None = None

    for i in range(n - pat_len + 1):
        # Check level match.
        if not _levels_match(runs, i, pattern):
            continue

        # Check duration tolerance (if enabled and template exists).
        if tolerance_us > 0 and template_durations is not None:
            if not _durations_match(runs, i, template_durations, tolerance_us):
                continue

        # Compute total span.
        total_dur = sum(runs[i + j].dur_us for j in range(pat_len))
        matches.append(
            {
                "index": i,
                "t_us": runs[i].t_us,
                "dur_us": total_dur,
            }
        )

        # Set template from first match.
        if tolerance_us > 0 and template_durations is None:
            template_durations = [runs[i + j].dur_us for j in range(pat_len)]

    return matches


def _levels_match(
    runs: Sequence[Run], start: int, pattern: list[int]
) -> bool:
    """Check whether run levels starting at *start* match *pattern*."""
    for j, expected in enumerate(pattern):
        if runs[start + j].level != expected:
            return False
    return True


def _durations_match(
    runs: Sequence[Run],
    start: int,
    template: list[float],
    tolerance_us: float,
) -> bool:
    """Check whether each run's duration is within tolerance of template."""
    for j, ref_dur in enumerate(template):
        if abs(runs[start + j].dur_us - ref_dur) > tolerance_us:
            return False
    return True
