# tomogui — Agent Context

Instructions for driving `tomogui-cli` headlessly from a pystream agent.
This file ships **inside the pystream package** — deploys automatically
with every install; no per-machine setup.

## Tool-call budget — READ THIS FIRST

pystream caps agent turns at ~10 tool rounds. Do NOT burn them on
verification. Real reconstructions take **minutes**, so:

- **Every `bash` call that runs `ssh` + `conda run` MUST pass
  `timeout=600` or higher** (default is 30 s, will time out mid-recon
  and look like failure). For a Try, 600. For a Full batch, 1800.
- Do NOT `--help` / `--version` / `ls` / `find` to "verify". Trust
  paths and CLI presence. If it's wrong you'll see the error.
- Do NOT `status --json` before every batch. The CLI is idempotent.
- Do NOT re-read this doc mid-turn. You already have it.
- If you're on round 5+ without having run the actual batch command,
  STOP and ask the user what to clarify.

## Pick the right subcommand — DO NOT default to `batch`

**One file** (or user names a specific `.h5`) → `ai-full FILE.h5`. One
tool round. This is the vast majority of asks.

**A folder with multiple `.h5` files** or the user says "batch",
"every scan", or names a directory → `batch --data-folder DIR
--phases ai,full`.

Do NOT wrap a single file in `batch --data-folder`. It works, but
adds noise and needs an extra `--data-folder` arg. `ai-full` is
one word shorter and does the same thing for one file.

## The two commands you'll actually run

### Single file with AI COR + full recon

```bash
ssh <USER>@<HOST> "bash -lc 'source ~/conda/anaconda/etc/profile.d/conda.sh \
    && conda activate tomoguiAI \
    && tomogui-cli ai-full /path/to/scan.h5 \
        --model /home/beams/USERTXM/conda/anaconda/envs/tomoguiAI/lib/python3.11/site-packages/tomogui/AImodels/datav2_518_full_finetune/epoch_10.pth \
        --gpu 1 --json'"
```

### A whole folder

```bash
ssh <USER>@<HOST> "bash -lc 'source ~/conda/anaconda/etc/profile.d/conda.sh \
    && conda activate tomoguiAI \
    && tomogui-cli batch --data-folder /path/to/folder \
        --phases ai,full \
        --model /home/beams/USERTXM/conda/anaconda/envs/tomoguiAI/lib/python3.11/site-packages/tomogui/AImodels/datav2_518_full_finetune/epoch_10.pth \
        --gpu 1 --json'"
```

**Call this via `bash` with `timeout=1800` (30 min) and BLOCK.**
Don't `setsid`, don't background with `&`, don't poll `pgrep` — the
`bash` tool now honors long timeouts, so just let the command run
to completion and read its final output. Backgrounding + polling
turns 1 tool round into 5+ and doesn't give you extra information.

Substitute `<USER>`, `<HOST>` from the user's ask. If they didn't
say GPU, use `1`. If they didn't name a model, use the canonical
path above (ships with the `tomoguiAI` conda env).

## Why the explicit `bash -lc 'source ... conda activate'` incantation

On USERTXM's account, `conda run -n tomoguiAI` doesn't work through
SSH because the login shell is tcsh, not bash. `conda run` needs its
init hook, which is only sourced for the right shell. The
`bash -lc 'source ~/conda/anaconda/etc/profile.d/conda.sh && conda
activate tomoguiAI && ...'` chain forces bash, sources conda's init,
activates the env, then runs the CLI. Do not "simplify" this away.

## Common defaults (use directly — do NOT verify)

| Thing | Value |
|---|---|
| Conda env for all tomogui/tomocupy work | `tomoguiAI` |
| Machine registry | `~/.tomogui/machines.json` |
| Bundled DINOv2 model weights | `/home/beams/USERTXM/conda/anaconda/envs/tomoguiAI/lib/python3.11/site-packages/tomogui/AImodels/datav2_518_full_finetune/epoch_10.pth` |
| Per-file tomocupy flag store | `<data_folder>/recon_params.json` |
| Chosen COR store | `<data_folder>/rot_cen.json` |
| Try output | `<data_folder>_rec/try_center/<proj>/*.tiff` |
| Full output (h5nolinks default) | `<data_folder>_rec/<proj>_rec.h5` |

The `epoch_10.pth` model is bundled with the `tomoguiAI` conda env's
`site-packages/tomogui/AImodels/` — it's ALWAYS present when
`tomoguiAI` is installed. Never search for it.

## CLI subcommands

```
tomogui-cli status <data_folder> [--json]
tomogui-cli try <file.h5> --cor N | --auto  [--gpu N]
tomogui-cli full <file.h5> [--cor N]  [--gpu N]
tomogui-cli ai-cor <file.h5>  --model PATH  [--seed N]  [--gpu N]
tomogui-cli ai-full <file.h5> --model PATH  [--seed N]  [--gpu N]
tomogui-cli batch (--data-folder DIR | --file F ... | --files-from list.txt)
                  --phases ai,full,tomolog,try
                  [--model PATH] [--gpu N] [--pattern *.h5] [--json]
tomogui-cli cor {get FILE | set FILE COR | list --data-folder DIR}
tomogui-cli view <target> [--slice N | --slices SPEC | --info [--stats]]
                  [--out PATH|-] [--vmin V --vmax V | --pct-lo 5 --pct-hi 95]
                  [--cmap gray] [--json] [--interactive]
tomogui-cli tomolog <file.h5> --url SLIDES_URL [--auto-contrast] [--gpu N]
```

`--json` is available on `status`, `cor get`, `cor list`, `ai-cor`,
`ai-full`, `batch`, and `view --info` — prefer it, parse it directly.

Common cross-cutting flags: `--recon-way {recon,recon_steps}`
(use `recon_steps` iff user mentions phase / Paganin),
`--extra "flags"` for tomocupy passthrough,
`--quiet` to capture output rather than stream.

## Showing a reconstructed slice back to the user

Use **`tomogui-cli view`** — the CLI has a built-in slice extractor +
renderer with autoscaling. No more hand-rolled h5py/tifffile scripts.

`<target>` can be any of:
- The source projection `.h5` (auto-resolves to its `_rec.h5` output)
- A full-recon `.h5` directly
- A TIFF-stack directory
- A `try_center/<proj>/` directory for try-recon previews

### Metadata only (fast, JSON)

```bash
ssh <USER>@<HOST> "bash -lc 'source ~/conda/anaconda/etc/profile.d/conda.sh \
    && conda activate tomoguiAI \
    && tomogui-cli view /data/scan.h5 --info --stats --json'"
```

Returns `{source, kind, n_slices, shape, stats:{min,max,mean,pct_5,pct_95}}`.
Use for the "how big is it, is it sensible" quick check. `timeout=60`.

### Middle slice as PNG — the common case

```bash
ssh <USER>@<HOST> "bash -lc 'source ~/conda/anaconda/etc/profile.d/conda.sh \
    && conda activate tomoguiAI \
    && tomogui-cli view /data/scan.h5 --slice mid --out /tmp/mid.png \
        --pct-lo 5 --pct-hi 95 --cmap gray'"
```

Autoscales to the 5–95th percentile (skip `--pct-*` to accept defaults).
`--out /tmp/mid.png` writes on the REMOTE host — then `scp` it back and
render locally with `view_detector_image`. `timeout=120`.

### Stream the PNG directly back (no scp)

```bash
ssh <USER>@<HOST> "bash -lc 'source ~/conda/anaconda/etc/profile.d/conda.sh \
    && conda activate tomoguiAI \
    && tomogui-cli view /data/scan.h5 --slice mid --out - --quiet'" \
    > /tmp/mid.png
```

`--out -` writes PNG bytes to stdout; `--quiet` suppresses the "wrote…"
line so nothing corrupts the byte stream. One tool round.

### Multi-slice export (e.g. sanity-check the whole volume)

```bash
tomogui-cli view /data/scan.h5 --slices every:64 --out /tmp/preview_dir/
```

Or `--slices A:B:S`, `--slices A,B,C`, `--slices all`.
Written to `<dir>/slice_<index>.png`, zero-padded.

### Interactive viewer (rare — only if user explicitly asks for GUI)

`tomogui-cli view /data/scan.h5 --interactive` launches the full
tomogui Qt GUI focused on this file. **Only use over SSH if X11 is
forwarded AND the user explicitly asked for the GUI**; otherwise the
default headless PNG extraction is what you want.

## Decision tree for "reconstruct" requests

1. **File or folder named?**
   - Single `.h5` file → use `ai-full FILE.h5`.
   - Folder / multiple files / user says "batch" → use `batch --data-folder DIR`.
2. **Machine + user named?** → yes, `ssh USER@HOST`. no, ask.
3. **AI / auto / find center in the ask?** → `ai-full` (single) or
   `batch --phases ai,full` (folder). If the user says "just full,
   COR is already set", use `full` (single) or `batch --phases full`.
4. **Run** the command via `bash` with `timeout=1800`. **Block on it.**
   No setsid, no `&`, no pgrep polling. The `bash` tool's `timeout`
   parameter is what you use to wait for long jobs.
5. **If the user asked for an image / slice / preview**, extract via
   `tomogui-cli view <target> --slice mid --out -` streamed over SSH
   into a local PNG, then `view_detector_image`. NOT via hand-rolled
   h5py — use the CLI's built-in `view`.

If on tool round 5 without step 4 done → STOP, ask the user.

## Failure handling — STOP, don't loop

| symptom | do this |
|---|---|
| `bash` returns `command timed out after Ns` | you didn't set `timeout` high enough. Ask user if the recon should still be running remotely; do NOT re-launch. |
| non-zero exit + JSON `error` field | quote the error, STOP |
| stderr `--model` file missing | check the canonical path; if still missing, STOP |
| stderr `CUDA out of memory` | retry ONCE with `--extra "--binning 2"`. If still fails, STOP |
| `No such file: X.h5` | acquisition hasn't finished; STOP |
| any other error | quote it, STOP. Do NOT loop diagnosing. |

## Python API (alternative to CLI)

```python
from tomogui import headless as H
sess = H.Session(data_folder="/data/session", model_path=<canonical>, gpu=1)
H.run_batch(H.list_h5(sess.data_folder), sess, phases=("ai", "full"))
```

Use only when already inside `tomoguiAI` python. For SSH-driven work
the CLI is simpler.

## Hard "never do this" list

- Never launch bare `tomogui` — it's the Qt GUI, needs display.
- Never `find /` for weights — use the canonical path.
- Never `--help` a subcommand to see flags — this doc IS the reference.
- Never re-read this file mid-turn — you already have it.
- Never install packages into `tomoguiAI` — report missing deps, STOP.
- Never retry a failed recon more than once — second failure → STOP.
- Never run `bash` with default 30 s timeout for SSH+conda work — the
  child will time out mid-recon and you'll waste 5 iterations "debugging"
  a command that was actually still running remotely.
- Never `setsid` / `nohup` / `&` a recon to "make it non-blocking",
  then poll with `pgrep` + `tail /tmp/log`. The `bash` tool's
  `timeout=1800` parameter IS your non-blocking mechanism. Blocking
  with a long timeout is one tool round; the background+poll dance is
  five rounds and gives you no additional information.
- Never wrap a single file in `batch --data-folder`. Use `ai-full FILE.h5`
  (or `full FILE.h5` when COR is already set). `batch` is for folders.
- Never hand-write `python -c "import h5py..."` to extract a slice.
  Use `tomogui-cli view <target> --slice mid --out -` — it handles
  H5 vs TIFF stacks, autoscales contrast, renders to PNG, and streams
  to stdout for scp-less pipelines.
- Never launch `tomogui-cli view --interactive` unless the user
  explicitly asked for the GUI AND the SSH session has X11 forwarding.
  The default (extract PNG) is what you almost always want.
