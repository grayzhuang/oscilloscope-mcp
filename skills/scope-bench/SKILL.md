---
name: scope-bench
description: Drive a bench oscilloscope over SCPI/LAN via the oscilloscope-mcp tools or its offline scripts — capture waveforms, measure edges/timing, check jitter/glitches/handshake causality, and dump raw voltages for statistics that exceed the MCP response budget. Use when the user mentions 示波器/scope/oscilloscope, 抓波形/捕获/capture, 测频率/脉宽/占空比, 边沿/edge, 抖动/jitter, 毛刺/glitch, 触发/trigger, 通道设置/V/div, RTL 仿真与实测对比, or debugging a digital signal on a real instrument.
---

# scope-bench — oscilloscope workflow layer

Sits between the agent and two execution paths that share one validated
library (`oscilloscope_mcp`):

1. **MCP tools** (12): `scope_query` `scope_screenshot` `scope_trigger`
   `scope_measure` `scope_measure_stat` `scope_channel` `scope_timebase`
   `scope_waveform` `scope_acquire` `scope_capture` `scope_compare`
   `scope_viewer` — when a host session has the server registered.
2. **Scripts** (`scripts/`, run with the dedicated env):
   `scope_cli.py doctor|dump` (online, file output) and
   `analyze.py glitch|jitter|pattern|causality|bus` (offline, file input).
   Run as:
   `conda run -n oscScope-mcp python skills/scope-bench/scripts/<x>.py …`

Everything model-specific lives in `references/models.md`; recipe
sequences in `references/recipes.md`. Read those before a first capture
on an unfamiliar instrument.

## Authorization gate (hard rule)

Any tool/script call that reaches the instrument configures or reads
real hardware. Check the user has authorized touching the scope for
this task before the first live call. Offline paths (`analyze.py`,
`scope_compare` with `hw_runs`/`hw_edges`) are always safe.

## Non-negotiable output contract

Every structured result carries `caveats[]`. **Read caveats before
quoting any number** — they flag bandwidth limits, sample-rate derating
(multi-channel), quantization loss, truncation, unmeasurable items.
A number quoted without its caveats is a wrong number.

## Choosing the path

| Need | Use |
|---|---|
| Quick look / few edges / measurements | MCP `scope_capture` / `scope_measure` |
| Raw voltage array on disk (large captures) | `scope_cli.py dump` |
| Event statistics (jitter/glitch counts, histograms) | `dump` + `analyze.py` — **not** the MCP waveform stream: it truncates at 400 runs, which silently ruins statistics |
| Handshake/CDC "B follows A" check | `scope_capture` both channels → save JSON → `analyze.py causality` |
| Human-viewable trace | `scope_viewer` (HTML) / `scope_screenshot` |
| Sim vs hardware diff | `scope_compare` (offline mode needs no scope) |

## Parameter rules (apply everywhere)

- **threshold_v / hysteresis_v**: the Schmitt comparator maps volts →
  0/1. Set `threshold_v` at the signal's mid-supply (CMOS ≈ VCC/2;
  never 0 V). `hysteresis_v` must exceed the noise band but stay under
  20 % of peak-to-peak or transitions get missed (a caveat fires; do
  not ignore it).
- **NORMal vs RAW** (semantics differ per family — see models.md):
  NORMal is the cheap read; RAW is full acquisition memory and requires
  the scope stopped (SINGLE sweep or `action='STOP'`). The driver
  enforces this — a RuntimeError is expected, not a bug; stop the scope
  and retry.
- **memory_depth / viewer `depth`**: bigger = longer window but slower
  transfer (`low` ≈30k / `mid` ≈300k / `high` ≈3M+ pts). Start `low`
  unless hunting rare events.
- **Sampling reality**: more channels displayed = lower per-channel
  sample rate (typically ÷2 at 2ch, ÷4 at 4ch); edge timing needs ≥5
  samples per period or it is decoration.

## Order of operations for a capture

1. `scope_cli.py doctor` (or `scope_trigger` read-only) — sanity-check
   connection, current setup, capability; read caveats about the
   current configuration.
2. Configure: `scope_channel` → `scope_timebase` → `scope_acquire` →
   `scope_trigger` (set what the task needs; skip the rest).
3. Acquire: `scope_capture` (declarative) or `scope_trigger
   action='SINGLE'` then `scope_waveform`.
4. Read caveats → then numbers. Save JSON/CSV to `data/` for anything
   worth re-analyzing.

## Hard limits

- `scope_query` is unvalidated raw SCPI: no capability checks, no
  caveats. Only for exploring commands the typed tools don't cover —
  never for numbers you'll report.
- Unsupported oscilloscope (no driver/profile): only `scope_query`
  works. Say so and stop; this skill cannot rescue an unsupported
  instrument.
- Not covered by design: XY/ROLL-mode analysis, scope-side MATH
  channels, fine-tuning non-EDGE trigger params, frequency-response
  sweeps.
