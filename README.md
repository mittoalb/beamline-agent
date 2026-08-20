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
