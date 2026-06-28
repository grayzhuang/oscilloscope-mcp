"""Threshold-quantize a raw voltage stream into a 0/1 digital level stream.

The agent should never consume raw ADC samples — it consumes an
RLE-encoded *threshold-quantized event stream* (see :mod:`.rle`,
:mod:`.edges`). The first stage of that pipeline lives here: convert a
list of volts into a list of digital levels (0 = low, 1 = high) using a
**Schmitt trigger** (hysteresis comparator).

Why hysteresis is mandatory
----------------------------
A single fixed threshold applied to a noisy edge fires repeatedly as the
signal jitters across the threshold during the transition, fragmenting
one real edge into a burst of useless micro-pulses. A Schmitt trigger
fixes this by using two thresholds and only switching state once the
signal has crossed *past* the threshold by a margin.

Exact convention (documented for reproducibility)
-------------------------------------------------
Given ``threshold_v`` (centre) and ``hysteresis_v`` (total band width):

- ``v_high = threshold_v + hysteresis_v / 2``  (rising threshold)
- ``v_low  = threshold_v - hysteresis_v / 2``  (falling threshold)

State machine, evaluated per sample in order:

- A **rising** transition (0 → 1) requires the sample to reach or exceed
  ``v_high``.
- A **falling** transition (1 → 0) requires the sample to drop to or
  below ``v_low``.
- While the sample sits *between* ``v_low`` and ``v_high`` the level is
  held at its previous value (this is the noise-immune dead band).

Initial level (before any transition) is decided by a plain comparison
against the centre ``threshold_v``: ``volts[0] >= threshold_v`` → 1 else
0. This gives a deterministic, intuitive starting state; subsequent
samples obey the hysteresis machine above.

A ``hysteresis_v`` of 0 degrades gracefully to a single-threshold
comparator (``v_high == v_low == threshold_v``), which is exactly the
fragmentation-prone behaviour the caveat in
:func:`oscilloscope_mcp.helpers.caveat_calc` warns about.
"""

from __future__ import annotations

from typing import Sequence


def quantize(
    volts: Sequence[float],
    threshold_v: float,
    hysteresis_v: float = 0.0,
) -> list[int]:
    """Convert a voltage stream into a 0/1 digital level stream.

    Parameters
    ----------
    volts : sequence of float
        Per-sample voltages (already scaled to volts by the driver).
    threshold_v : float
        Comparator centre threshold in volts.
    hysteresis_v : float
        Total width of the hysteresis band in volts (rising threshold is
        ``threshold_v + hysteresis_v/2``, falling is
        ``threshold_v - hysteresis_v/2``). Must be >= 0.

    Returns
    -------
    list[int]
        One 0/1 level per input sample, same length as ``volts``.
    """
    if hysteresis_v < 0:
        raise ValueError(f"hysteresis_v must be >= 0, got {hysteresis_v}")
    if not volts:
        return []

    half = hysteresis_v / 2.0
    v_high = threshold_v + half
    v_low = threshold_v - half

    # Initial level: plain comparison against the centre threshold.
    level = 1 if volts[0] >= threshold_v else 0
    out: list[int] = [level]

    for v in volts[1:]:
        if level == 0:
            if v >= v_high:
                level = 1
        else:  # level == 1
            if v <= v_low:
                level = 0
        out.append(level)
    return out


def peak_to_peak(volts: Sequence[float]) -> float:
    """Return ``max(volts) - min(volts)`` (signal amplitude). 0 for empty
    or flat input. Used by the detection-miss caveat rule.
    """
    if not volts:
        return 0.0
    return float(max(volts)) - float(min(volts))
