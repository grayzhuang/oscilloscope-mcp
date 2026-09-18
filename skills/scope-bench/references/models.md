# Model differences & troubleshooting — append-only

One section per instrument family. Record only facts the tool responses
and `caveats[]` cannot express; behaviour already surfaced by the tools
stays there. **Append, never rewrite** — this file is the accumulated
bench memory.

## Connection matrix

| Family | SCPI port | Auto-detect | Notes |
|---|---|---|---|
| RIGOL DS1000Z | 5555 | by `*IDN?` | port 5025 also probed when unset |
| ZLG ZDS1000 | 5025 | by `*IDN?` | port 5555 also probed when unset |

If both probe ports answer `*IDN?` on the same host (unusual), set
`SCOPE_MCP_MODEL` to disambiguate.

## RIGOL DS1000Z (DS1104Z verified; DS1054Z same code path, not bench-verified)

- **NORMal waveform read** = screen decimation, ~1200 points. Fine for
  edge lists; useless for glitch hunting below one screen-pixel width.
- **RAW waveform read** = full acquisition memory (up to 24 Mpts),
  transferred in 250 k-point chunks — expect tens of seconds at max
  depth. Scope must be STOP/TD; the driver enforces this.
- Screenshot formats: PNG/BMP/TIFF (profile `screenshot_formats`).
- Trigger types beyond EDGE and non-NORMal acquisition modes are
  implemented per programming manual and unit-tested but **not
  bench-verified** — treat unexpected responses as findings, don't
  paper over them.

## ZLG ZDS1000 (ZDS1104 verified, fw 1.2.67)

- **`SCREen` read (NORMal) returns the FULL memory** (= memory-depth
  points), readable while running — unlike RIGOL's ~1200-point screen
  read. The two vendors' "NORMal" are different words for different
  things; the caveats wording differs accordingly.
- **`MEMOry` read (RAW)** requires STOP (returns 0 bytes while
  running; driver raises). After a run-state change the first read can
  transiently return an empty block — the driver retries 3×; if it
  still fails, STOP → wait 1 s → retry.
- Run-state queries (`:GLOBal:RUN:STATe?`) answer with 0.24–0.42 s
  latency; the driver polls — don't add your own zero-delay retry loop
  on top.
- Screenshots are **BMP only**. There is no channel-label SCPI;
  screenshot `channel_labels_applied` will be empty by design.
- **`:MEASure:PERiod? <src>` returns literal `0` on fw 1.2.67** (verified
  live 2026-09-18; FREQUENCY answers normally). The driver passes the 0
  through — it is the firmware not reporting, not a real zero period.
  Derive period as `1 / FREQUENCY`; treat any PERIOD=0.0 from this
  family as "unmeasurable".
- ASCII responses end with `\r\n` (handled internally).
- `FORCE` trigger has no verified SCPI equivalent — use NORMAL/AUTO
  sweep or external trigger instead.

## Adding a model (agent-facing checklist)

1. Same family, new model number → zero skill/script changes; verify
   `*IDN?` matches the family profile's `idn_match`, else add a profile.
2. New vendor → profile YAML + `Scope` driver subclass + one registry
   line (see repo AGENTS.md), then bench-verify; add a section above
   only for facts the tools can't express.
3. No driver at all → only raw `scope_query` works (unvalidated, no
   caveats). State this limit to the user rather than improvising SCPI.
