# Recipes — complete call sequences by goal

Sequences use MCP tool names and script invocations interchangeably; a
script line means:

```
conda run -n oscScope-mcp python skills/scope-bench/scripts/<x>.py …
```

Artifacts go to `data/` (never committed). Every recipe ends the same
way: **read `caveats[]` before reporting numbers.**

## R1 — First contact / health check (read-only)

Goal: verify the instrument is reachable and understand its current
state and capability before touching anything.

```
scope_cli.py doctor                 # env → IDN → capability → full setup snapshot
scope_cli.py doctor --save          # + persist data/<model>_setup_<ts>.yaml
```

MCP-only alternative: `scope_trigger()` with no args (read-only state),
`scope_measure(["VPP"], source="CHAN1")` to see if a signal is present.

When to stop: if `caveats` already complains (e.g. 20M BW limit engaged,
4-channel sample-rate derating) fix the setup before measuring.

## R2 — Configure + measure a digital signal

Goal: frequency / pulse width / duty / VPP of one channel, fastest path.

1. `scope_channel(channel=1, scale_v_per_div=…, coupling="DC", probe=…)`
   — vertical so the signal fills most of the screen without clipping.
2. `scope_timebase(s_per_div=…)` — ~2–3 periods visible.
3. `scope_trigger(mode="EDGE", params={"source":"CHAN1","slope":"POS"})`.
4. `scope_measure(items=["FREQUENCY","PWIDTH","NDUTY","VPP"],
   source="CHAN1")` — or `scope_capture` with `measurements=[…]` to do
   configure+arm+measure in one call.
5. Statistics over many acquisitions: `scope_measure_stat(items=[…],
   reset=true)`.

Null values = un-measurable (flat trace / no trigger) — named in caveats.

## R3 — Quick waveform / edge list (small captures)

Goal: what does the signal look like, when are the edges.

```
scope_waveform(source="CHAN1", threshold_v=1.65, hysteresis_v=0.3)
```

Returns `runs` + `edges` (µs, trigger at t=0). Fine for a handful of
transitions; see R4 when you need statistics or the full memory.

## R4 — Full-memory raw dump (statistics-grade)

Goal: jitter / glitch / interval statistics, long records, offline
re-analysis. Use this whenever event counts matter — the MCP waveform
stream truncates at 400 runs.

```
scope_trigger(action="SINGLE")        # arm; wait for trigger, scope stops
scope_cli.py dump CHAN1 --mode RAW    # → data/<model>_waveform_<ts>_chan1.csv + .meta.json
analyze.py jitter -i data/<…>_chan1.csv --kind RISE
```

Multi-channel: `dump CHAN1 CHAN2 CHAN3` (one CSV+meta per channel).
Screen-quality is enough (`--mode NORMal`, no STOP required) when the
event count is small. Re-quantization thresholds for CSV analysis:
`--threshold-v` / `--hysteresis-v` (same Schmitt convention).

## R5 — Event statistics (glitch / jitter / pattern)

Goal: quantify misbehavior. Input = saved `scope_capture` JSON or a
dump CSV (R4).

```
analyze.py glitch   -i cap.json --min-width-us 0.05     # pulses shorter than spec
analyze.py jitter   -i cap.json --kind RISE             # interval mean/σ/min/max/p99 + histogram
analyze.py pattern  -i cap.json --pattern 0,1,0,1 --tolerance-us 0.2   # protocol frame template
```

Pick `--min-width-us` from the signal's spec (e.g. setup/hold minimum),
not from what you see. `--md` renders a markdown report instead of JSON.

Volts-domain analysis (dump CSV only — needs the raw samples):

```
analyze.py fft      -i dump_chan1.csv --peaks 8         # spectral peaks (osc purity, switching noise)
analyze.py hist     -i dump_chan1.csv --bins 64         # level distribution (floating / bimodal levels)
analyze.py envelope -i dump_chan1.csv --points 400      # min/max overview of a long capture
plot.py  wave       -i dump_chan1.csv [dump_chan2.csv]  # PNG with trigger marker (HTML fallback)
```

FFT caveat: DC offsets leak past the skipped bin 0 — remove the mean
first (or interpret the low-frequency peaks as leakage, not signal).

## R6 — Handshake / CDC causality check

Goal: prove "B always answers A within N µs" (req→ack, clock→data…).

1. Probe A on CHAN1, B on CHAN2; configure both channels.
2. `scope_capture(channels=["CHAN1","CHAN2"], sweep="SINGLE", …)` and
   save the result JSON (host-dependent; or use R4 dumps per channel
   into one file each and analyze the JSON you assembled).
3. `analyze.py causality -i cap.json --a CHAN1 --b CHAN2
   --max-delay-us <budget> [--a-kind RISE --b-kind FALL]`

Output lists every violation with `a_t_us` and the nearest B edge;
`matched/total_a` is the pass criterion.

## R7 — Parallel bus decode

Goal: multi-bit bus value over time (data bus, control lines).

```
scope_capture(channels=["CHAN4","CHAN3","CHAN2","CHAN1"], sweep="SINGLE")
# → tool result already contains bus_runs (channels MSB-first)
# offline / larger captures:
analyze.py bus -i cap.json --channels CHAN4,CHAN3,CHAN2,CHAN1
```

`--channels` order is MSB→LSB and defines the packing (`value = Σ
level_i << (n-1-i)`). Invert the order and every value is garbage —
double-check against a known state (idle value).

## R7b — Measurement trend over time

Goal: how a quantity drifts (warm-up, supply sag, intermittent fault
needs a long observation window). Polls scope-side measurements at a
fixed cadence — no waveform transfer, so intervals stay cheap.

```
scope_cli.py meas-log --items VPP,FREQUENCY --source CHAN1 \
    --interval 1 --duration 60        # → data/<model>_measlog_<ts>.csv + stats
plot.py trend -i data/<model>_measlog_<ts>.csv   # per-item trend PNG
```

Ctrl+C ends the run early but still prints the summary. Unmeasurable
samples land as empty cells; per-item stats skip them. An item that
reads a suspicious constant (e.g. a literal 0 period on some firmware)
is a firmware quirk — see models.md, don't log it for an hour.

## R8 — RTL sim vs hardware diff

Goal: the project's core loop — golden edges from simulation vs real
pins. Fully scripted now:

```
# 1. Reference: VCD from the simulator (or a known-good dump CSV)
vcd2ref.py -i sim.vcd --signal top.uart.tx -o ref.json
# 2. Hardware: capture JSON or dump CSV (both work as --hw)
scope_capture(channels=["CHAN1"], sweep="SINGLE")   # save the result JSON
scope_cli.py dump CHAN1 --mode RAW                  # or dump the full memory
# 3. Align + diff + report
compare_rtl.py --ref ref.json --hw capture.json --hw-channel CHAN1 \
    --tolerance-us 0.05 --md report.md
```

**Alignment is your job**: hardware edges are trigger-aligned (t=0 =
trigger), the simulation axis is the testbench's. Find hw's first edge
(`vcd2ref.py -i dump.csv` prints `t_first_us`), then shift the reference
with `--ref-offset-us` so both axes share an origin — or expect every
edge to show up as missing/added.

Returns matched / shifted (with per-edge `delta_us`) / missing / added /
`first_divergence_us`. Set `tolerance_us` to a few sample intervals —
tighter than the sample rate produces pure noise. `vcd2ref` also accepts
a dump CSV (one board as golden reference for another board).

Alternatively call the `scope_compare` MCP tool directly with
`reference_edges` + `hw_edges`/`hw_runs` (offline, no scope).

## Troubleshooting quick table

| Symptom | First check |
|---|---|
| No tools / connection refused | host env var, port (see models.md §connection), `scope_cli.py doctor` |
| RAW read raises "must be stopped" | expected — `scope_trigger(action="STOP")` or SINGLE, retry |
| Flat waveform (peak-to-peak 0) | channel displayed? probe tip actually on the net? threshold inside the swing? |
| FREQUENCY null | no stable trigger — fix trigger source/level first |
| Truncation caveat in waveform | too many runs → switch to R4 dump path |
| Numbers look quantized | read the caveats: sample-rate derating / BW limit engaged |
