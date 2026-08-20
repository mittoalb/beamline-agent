# beamline-agent

AI-agent chat + sub-agent dispatch + status monitor + image viewers for
APS beamline tools. Currently hosted in-process by
[pystream](https://github.com/mittoalb/pystream) — pluggable into any
Qt app via the `HostContext` protocol.

## What's inside

- **Chat panel** — the pystream "AI Agent" dock; talks to Anthropic or
  OpenAI, with tool-use loop + parallel turns + persistent history.
- **Sub-agent dispatch** — `spawn_subagent(kind, task)` delegates to
  specialists (reconstruction, physicist, chemist, beamline_operator).
- **Agents panel** — live tree of every AI agent process running on the
  beamline (main + spawned sub-agents + cross-machine workers).
- **Console** — wire trace of every tool call + result, with an
  in-flight timer for long-running work.
- **Image viewer** — agent-only PNG/TIFF/NPY viewer (no toolbar
  button; the agent calls `view_image(path)`).
- **HDF5 viewer helper** — same for `.h5` files (via the host).
- **Task recorder** — captures motor sequences + detector frames so
  the agent can replay them later.
- **Learned-note store** — `save_learned_note` writes to the
  package's git checkout so notes propagate on `git pull`.
- **Instruction docs** (`context_docs/*.md`) ship inside the package
  and auto-deploy to `~/.pystream/docs/` on first launch.

## Install

```bash
pip install beamline-agent
```

Or from source:

```bash
git clone <this-repo>
cd beamline-agent
pip install -e .
```

## Use from pystream (the current primary host)

pystream lists `beamline-agent` as an optional dep. Install both and the
"AI Agent" dock, `👥 Agents`, `📜 Console`, and `🎥 Task Rec` buttons
appear in pystream automatically. Don't install `beamline-agent` and
pystream keeps working — just without those buttons.

## Use from another Qt app

```python
from beamline_agent import mount, HostContext

class MyApp(QMainWindow):
    def __init__(self):
        super().__init__()
        ...  # your normal main window setup
        host = HostContext(
            main_window       = self,
            live_frame        = lambda: self.get_current_frame(),
            frame_signal      = self.new_frame,          # pyqtSignal(np.ndarray)
            open_hdf5_viewer  = self._open_hdf5,
            open_plugin       = self._open_my_plugin,
            active_beamline   = "bl32ID",
        )
        mount(host)   # adds the chat dock + toolbar buttons to `self`
```

The `HostContext` protocol lets the agent use whatever the host offers
and gracefully degrade for anything absent (a host that doesn't provide
`live_frame` just won't have the live-image tool in the catalog).

## Which tool packages the agent sees — `~/.pystream/agent_packages.json`

At chat startup the agent reads `~/.pystream/agent_packages.json`, a
plain user-editable list of Python package names. For each entry the
agent tries to `import` it, then probes for what the package offers by
convention:

- **`<pkg>.data_dir()`** returns a directory → the tree is recursively
  mirrored into `~/.pystream/procedures/`. (Pattern used by
  [beamlines-procedures](https://github.com/mittoalb/beamlines-procedures).)
- **`AGENTS.md`** at the package's repo root → copied to
  `~/.pystream/docs/<pkg>_AGENTS.md` so the agent can `read_file` it
  via a predictable path.

Packages not installed on this host are **skipped silently** — a
superset list is safe. Nothing about which packages to look at is
hardcoded inside beamline-agent; this file is the single source of
truth. To add or remove a package, edit the file — no code change.

**First launch** writes an empty template to
`~/.pystream/agent_packages.json` with just an explanatory `_comment`
and `"packages": []`. The user fills in the list.

**An example config** ships inside the package at
[`src/beamline_agent/examples/agent_packages.json`](src/beamline_agent/examples/agent_packages.json)
with the common APS beamline packages already listed
(`pystream`, `bl_gui`, `xanes_gui`, `tomogui`, `beamlines_procedures`).

Copy it into place with:

```bash
python -c "from beamline_agent.context import copy_example_agent_packages; copy_example_agent_packages()"
```

or manually:

```bash
python -c "import beamline_agent, os; print(os.path.join(os.path.dirname(beamline_agent.__file__), 'examples', 'agent_packages.json'))" \
  | xargs -I {} cp {} ~/.pystream/agent_packages.json
```

Then edit `~/.pystream/agent_packages.json` to keep only the packages
you have installed. Restart pystream — the new set of tools + procedures
is discovered on next chat.
