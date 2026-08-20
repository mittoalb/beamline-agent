# bl_gui — Agent Context

Instructions for driving `bl_gui` **headlessly** via `bl-cli`. The
Qt GUI (`bl_gui bl32id.json`) is untouched and stays available for
interactive use on the beamline console; `bl-cli` is a parallel
entry point so an agent can query the layout and drive motors
without a display (over SSH, from a container, from another Python
process — anywhere).

## Environment

- **Conda env: `pystream`** (has PyQt5, pyepics, pvapy).
- `bl-cli` is registered as a console script by
  `pip install -e /home/beams/AMITTONE/Software/bl_gui/`. If it
  isn't on `$PATH`, that install hasn't run in the current env.
- Live EPICS reads/writes need `EPICS_CA_ADDR_LIST` (or
  auto-discovery). Layout-JSON operations (`bl-cli layout …`)
  don't touch PVs and work anywhere.

## Two entry points

- **`bl-cli`** — command-line, one subprocess per invocation.
  Prefer this for agent use — a hung EPICS call cannot wedge the
  agent, and `--json` gives a clean parse.
- **`from bl_gui import headless as h`** — in-process Python API,
  same operations, no argparse cost. Use when looping over the
  layout or chaining reads inside pystream.

Both share exactly one implementation — output shapes match.

## The CLI at a glance

```
# Layout introspection (no PVs touched)
bl-cli layout list                          — layout summary
bl-cli layout motors [--name bl32id]        — flat motor list (label + PV + panel)
bl-cli layout panels [--name bl32id]        — panel summary
bl-cli layout actions [--name bl32id]       — saved CfgButtons

# One-off caget / caput
bl-cli motor get <PV>                       — caget -t <PV>
bl-cli motor rbv <MOTOR_PV>                 — <MOTOR_PV>.RBV
bl-cli motor set <PV> <VALUE> [--timeout 5] — caput <PV> <VALUE>
bl-cli motor wait <MOTOR_PV> [--timeout 30] — poll .DMOV until settled

# Coordinated energy move via the ZP calibration table
bl-cli energy interp <keV> [--no-regime]         — compute targets, no caput
bl-cli energy set <keV> [--dry-run] [--no-regime] — sync-caput every motor

# pystream QGMax bridge (writes ~/.pystream_qgmax_request.json)
bl-cli qgmax trigger
bl-cli qgmax status

# Scintillator-screen autofocus (variance-of-Laplacian sweep)
bl-cli autofocus --motor <PV> --image-pv <PV> [--half-range 1.0] [--steps 21]

# Nano/Micro regime (persists to ~/.bl_gui/regime.txt)
bl-cli regime get
bl-cli regime set (nano|micro)
```

Every subcommand accepts `--json` for a machine-readable payload;
prefer `--json` whenever parsing. Nonzero exit code on failure so
shell chains do the right thing.

## Canonical invocation patterns

**"What motors are on 32-ID?"** →
```
bl-cli layout motors --name bl32id --json
```
Returns a list of `{panel_key, panel_title, tab, label, pv, custom, twv}`.
Filter by panel_title in the agent to answer "what zone-plate motors are
there" without another CLI call.

**"Move ZP Z to 1.234"** →
```
bl-cli motor set 32idbTXM:mcs2:c1:m15 1.234
bl-cli motor wait 32idbTXM:mcs2:c1:m15 --timeout 30
```
Always issue `wait` after `set` when downstream steps depend on the
motor having settled. Look up the PV via `layout motors` first — never
hard-code from memory (the layout is user-editable).

**"Change energy to 9 keV coordinating the ZP + Queensgates"** →
```
bl-cli energy set 9.0 --dry-run --json      # inspect the plan first
bl-cli energy set 9.0 --json                # then commit
```
Reads `~/.bl_gui/bl32id_zp_calibration.json`, polyfit-interpolates
each column at the target energy, sync-caputs every included motor.
Requires ≥2 calibration points in the file — otherwise every row
comes back status `error` with `reason` "only N cal point(s)".

**"Refocus the scintillator"** → after asking permission, since this
moves a motor over a range:
```
bl-cli autofocus --motor 32idbTXM:nf:m4 --image-pv 32idbSP1:Pva1:Image \
                 --half-range 0.5 --steps 15
```

**"Trigger a QGMax pass"** →
```
bl-cli qgmax trigger --json
# pystream picks up ~/.pystream_qgmax_request.json and runs one cycle.
bl-cli qgmax status --json   # poll until running=false
```

## When to use bl-cli

- Reading motor / detector PV state that isn't in the layout
  (`bl-cli motor get`, `bl-cli motor rbv`).
- Enumerating what the beamline has (`bl-cli layout motors|panels`)
  — better than guessing PV names from prior conversations.
- Coordinated moves whose logic already lives in bl_gui (energy
  calibration, autofocus).
- Anything that would require launching the full `bl_gui` GUI on
  a display just to click one button.

## When NOT to use bl-cli

- **Don't launch the full GUI.** `bl_gui bl32id.json` is
  interactive-only. Every headless operation goes through `bl-cli`
  or `bl_gui.headless`.
- **Don't use `pvaccess.Channel.put()`** in any script you write.
  It has no timeout and wedges the worker pool. Always caput via
  `bl-cli motor set` or `bash: caput …`.
- **Don't caput to random PVs without user permission.** `bl-cli
  motor set` is a write — same permission bar as any other tool
  that moves hardware.
- **Don't edit `src/bl_gui/layouts/*.json` or files under
  `/home/beams/USERTXM/…`.** Templates + IOC-side patches are out
  of scope for the agent — read-only.

## Layout JSON schema — `bl-cli layout motors --json` shape

```json
[
  {
    "panel_key":   "Zone Plate::User Mode",
    "panel_title": "Zone Plate",
    "tab":         "User Mode",
    "label":       "ZP X",
    "pv":          "32idbTXM:mcs2:c1:m13",
    "custom":      false,
    "twv":         ""
  },
  ...
]
```

Motors with empty PVs are dropped from this list (they exist in the
raw JSON but the agent can't move them). Panel keys are
`"BaseTitle::TabName"` — user-duplicated panels get
`"BaseTitle#N::TabName"`.

## `bl-cli energy set/interp --json` shape

```json
[
  {
    "label":   "ZP X",
    "pv":      "32idbTXM:mcs2:c1:m13",
    "col":     1,
    "include": true,
    "target":  1.234567,
    "status":  "ok",         // or "skip", "error", "dry-run", "caput_failed"
    "reason":  ""            // populated on skip/error
  },
  ...
]
```

Status vocabulary:
- `ok` — caput succeeded (only from `energy set` without `--dry-run`).
- `dry-run` — would have caput; no PV touched.
- `ready` — computed target, ready to caput (only from `energy interp`).
- `skip` — deliberately excluded; see `reason` (Include=off, MICRO regime).
- `error` — no PV configured, no cal data, or numeric failure.
- `caput_failed` — caput rc≠0 or timed out.

## Gotchas (cribbed from bl_gui/AGENTS.md sections 11–12)

- **Motors don't follow caput** → check `.SET` (0 = normal, 1 = set-
  position mode), `.DISP` (disabled), `.HLS`/`.LLS`/`.LVIO` (limits).
  A motor that ignores caput is almost never a bl_gui bug.
- **`stringout` 40-char limit** — anything caputted to a stringout
  PV silently truncates at 40 bytes. If a filename doesn't take,
  it's this.
- **`caput` timeout** — every write uses a 5 s subprocess timeout.
  Slow moves aren't wrong; they just get logged as `caput_failed`.
  Read `.DMOV` separately if you care whether the move is still in
  flight.
- **Char waveforms** — `caget -S` decodes to a string; plain
  `caget` returns byte counts. `bl-cli motor get` uses `-t` which
  handles most cases, but for a waveform PV you may want to
  `bash: caget -S <pv>` instead.

## More detail

See `bl_gui/AGENTS.md` for the full deep-reference (widget
architecture, PV layer, layout save/load, calibration plumbing,
IOC patches, PV naming conventions at 32-ID). That doc's front
matter mirrors this one; the appendix has the 600 lines of
implementation notes for anyone refactoring bl_gui itself.
