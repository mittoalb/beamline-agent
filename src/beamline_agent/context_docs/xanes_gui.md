# xanes_gui — Agent Context

Instructions for driving `xanes_gui` **headlessly** via `xanes-cli`.
The Qt GUIs (`xanes-gui` / `python -m xanes_gui`) remain untouched
and available for interactive use; the CLI is a parallel entry point
so agents can drive scans without a display.

## Environment

- **Conda env: `pystream`** (has PyQt5, pyepics, pvaccess, h5py, numpy).
- The `tomoscan` env is only needed on the **remote host** the 3D
  scan SSHes into — NOT on the machine invoking `xanes-cli`.
- `xanes-cli` shells out to `xanes_energy.py` on the remote host via
  SSH+conda, exactly the way the GUI's Start button does. Same
  script path, same conda invocation.

## Two entry points

- **`xanes-cli`** — command-line, subprocess per invocation. Preferred
  for scripting and agent use.
- **`from xanes_gui import headless as H`** — pure-Python module (no
  Qt widgets, tiny event loop only for 2D signals). Use for
  in-process orchestration.

Both live inside the `xanes_gui` package that `pip install`s alongside
the `xanes-gui` GUI script — nothing extra to deploy.

## The CLI at a glance

```
xanes-cli status                                                — where are the config files, are they populated?
xanes-cli config show [--is-2d] [--json]                        — dump ~/.xanes_gui[_2d]_settings.json
xanes-cli config set KEY VALUE [--is-2d]                        — one-field update; VALUE is JSON literal or string

xanes-cli energies manual --start-keV 8.9 --end-keV 9.1 --step-eV 2 [--json]
xanes-cli energies edge   --element Cu --half-width-eV 100 --step-eV 2 [--json]
xanes-cli energies from-file PATH [--json]
xanes-cli energies save OUT.npy manual|edge|from-file <same flags>

xanes-cli edge get ELEMENT [--json]

xanes-cli 3d dry-run [--config PATH] [--repeat N] [--interval-min M] [--json]
xanes-cli 3d start   [--config PATH] [--repeat N] [--interval-min M]
                     [--qgmax-every N] [--json]

xanes-cli 2d dry-run [--config PATH] [--json]
xanes-cli 2d start   [--config PATH] [--json]
```

`--json` on every read-style subcommand for machine parsing. Every
action subcommand returns non-zero on failure; the reason goes to
stderr.

## Common workflows

### Preview an energy list before running

```bash
xanes-cli energies edge --element Cu --half-width-eV 200 --step-eV 2 --json
```

Prints n_points + first/last + full array. Nothing touches the beamline.

### Save a custom energy list for the next 3D scan

```bash
xanes-cli energies save ~/energies.npy edge --element Cu --half-width-eV 200 --step-eV 2
```

`xanes_energy.py` on the remote side picks up `~/energies.npy` if
it's fresh (mtime < 60 s old) and uses it instead of the XanesStart /
XanesEnd / XanesStep PVs.

### Fire a 3D XANES scan

```bash
# Preview the command first:
xanes-cli 3d dry-run --json
# Actually run it:
xanes-cli 3d start --repeat 3 --interval-min 15 --qgmax-every 5
```

`3d start` blocks until the remote scan finishes, streaming its stdout
back. If you're calling from a pystream agent tool, pass `timeout=1800`
(30 min) or higher — reconstructions take minutes; the default 30 s
`bash` timeout will chop the child before it's done. See
`tomogui.md` — same rule.

### Fire a 2D XANES scan

```bash
xanes-cli 2d start
```

Uses `~/.xanes_gui_2d_settings.json` unless `--config` is passed. Runs
in-process (no SSH). Blocks until done and prints the master HDF5
path on stdout.

## Python API for in-process use

```python
from xanes_gui import headless as H

# Energy generation
e = H.generate_energies_around_edge("Cu", half_width_eV=200, step_eV=2)
H.save_energies("~/energies.npy", e)

# 3D scan
cfg = H.DEFAULTS | H.load_settings(is_2d=False)      # merge user + defaults
cmd = H.build_3d_launch_command(cfg)                 # what would run
rc  = H.run_3d_scan(cfg, repeat_count=3, repeat_interval_s=900,
                    on_log=print)                    # actually run

# 2D scan
cfg2d = H.load_settings(is_2d=True)
master = H.run_2d_scan(cfg2d["pvs"], cfg2d["scan"], cfg2d["params"],
                       on_log=print,
                       on_progress=lambda i, t: print(f"{i}/{t}"))
```

## Anti-patterns

- Do NOT launch `xanes-gui` (the Qt GUI) headlessly — needs a display.
- Do NOT hand-write `ssh usertxm@... 'python xanes_energy.py'` — the
  full chain (bash -l -c, source conda.sh, conda activate tomoscan)
  is fragile; use `xanes-cli 3d start` instead.
- Do NOT edit `~/.xanes_gui_settings.json` in an editor while the
  GUI is open — it caches; use `xanes-cli config set` when the GUI's
  open, then reload from the GUI.
- Do NOT bypass QGMax auto-mode: if a scan is running with
  `--qgmax-every N`, another concurrent scan reading the same
  `qgmax_request.json` will fight it. Serialize scans.
- Do NOT delete `~/energies.npy` after starting the 3D scan — the
  remote `xanes_energy.py` reads it at each iteration.

## Failure modes

| symptom | what to do |
|---|---|
| `xanes-cli 3d start` returns rc=255 | SSH auth failed — verify keys with `ssh usertxm@HOST hostname` first |
| `remote host key verification failed` | `ssh-keyscan HOST >> ~/.ssh/known_hosts` once |
| `xanes-cli 2d start` errors "pvaccess timeout" | detector isn't publishing on the configured PV — check `list_status_pages` |
| `xanes-cli 2d start` errors "energies_eV empty" | 2D config's `scan.energies_eV` list is missing — open the 2D GUI once, set energies, save settings |
| CLI hangs indefinitely | The remote script is likely alive but silent — SSH stdout in log; check `pgrep -f xanes_energy.py` on the remote host |

## What NOT to do (agent-side)

- Never run `xanes-cli 3d start` from Röntgen's `bash` tool without
  `timeout=1800` or higher. The scan runs for minutes; default 30 s
  will phantom-fail.
- Never spawn a `reconstruction` sub-agent to run xanes — that
  sub-agent knows tomogui-cli, NOT `xanes-cli`. If you want a
  dedicated `xanes` sub-agent, add a `SUBAGENT_KINDS["xanes"]` entry
  with this doc as the prompt.
