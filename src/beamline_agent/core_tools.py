"""Core, beamline-agnostic tools for the AI Agent.

These tools are ALWAYS available to the agent, regardless of which
beamline is active (or whether any beamline is active at all). They
operate on data in `~/.pystream/` that isn't tied to any specific
facility.

Beamlines can still contribute their own tool catalog via
`provide_agent_context()` (see bl32ID/agent_tools.py); those merge on
top of what's defined here — see `pystream/agent.py::_load_tool_context`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable

# Host state directory. Defaults to ~/.pystream for backward compat
# with the pystream host; other hosts can override via HostContext.
PYSTREAM_HOME = os.path.expanduser("~/.pystream")

# Reuse the same store the Task Recorder writes to. Importing
# task_recorder here would create a Qt dependency in the tool-catalog
# module, so we hardcode the path (kept in sync manually) and rely on
# task_recorder's own one-time migration to move any legacy layout.
TASK_RECORDINGS_ROOT = os.path.join(PYSTREAM_HOME, "task_recordings")

# Personal-fallback location for learned notes when the pystream source
# checkout isn't detected (fresh pip install on a different machine).
# Notes there DO NOT get committed to git — the user will need to
# manually copy anything useful into the source tree.
LEARNED_NOTES_PATH = os.path.join(PYSTREAM_HOME, "_learned.md")


# ── low-level helpers ───────────────────────────────────────────────────

def _read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rows


# ── tools ───────────────────────────────────────────────────────────────

def tool_list_task_recordings() -> dict:
    """Enumerate recorded task sessions under
    ~/.pystream/task_recordings/. Structure:
        {"tasks": [
            {"name": <display>, "slug": <dir>,
             "sessions": [{"id": "YYYYMMDD_HHMMSS", "ts": <epoch>,
                           "moves": N, "opening_note": "...",
                           "closing_note": "..."}, ...]}, ...]}
    """
    try:
        if not os.path.isdir(TASK_RECORDINGS_ROOT):
            return {"tasks": [], "root": TASK_RECORDINGS_ROOT}
        tasks = []
        for slug in sorted(os.listdir(TASK_RECORDINGS_ROOT)):
            task_dir = os.path.join(TASK_RECORDINGS_ROOT, slug)
            if not os.path.isdir(task_dir):
                continue
            sessions = []
            for sess_id in sorted(os.listdir(task_dir)):
                sess_dir = os.path.join(task_dir, sess_id)
                jsonl = os.path.join(sess_dir, "actions.jsonl")
                if not os.path.isfile(jsonl):
                    continue
                rows = _read_jsonl(jsonl)
                start = next((r for r in rows if r.get("type") == "session_start"), {})
                end = next((r for r in reversed(rows) if r.get("type") == "session_end"), {})
                moves = sum(1 for r in rows if r.get("type") == "motor_move")
                sessions.append({
                    "id": sess_id,
                    "ts": start.get("ts"),
                    "task": start.get("element", slug),
                    "moves": moves,
                    "opening_note": start.get("opening_note", ""),
                    "closing_note": end.get("closing_note", ""),
                })
            if sessions:
                tasks.append({
                    "name": sessions[-1]["task"],
                    "slug": slug,
                    "sessions": sessions,
                    "session_count": len(sessions),
                })
        return {"tasks": tasks, "root": TASK_RECORDINGS_ROOT}
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}


def tool_save_learned_note(topic: str, content: str,
                            tool: str = "general") -> dict:
    """Append a durable note to the agent's own knowledge file, for
    future turns to benefit from. Called when the agent discovers
    something worth remembering: a new CLI flag, a corrected file
    path, a machine's shell quirk, a failure mode + workaround, a PV
    that behaves differently than documented.

    Writes to `~/.pystream/_learned.md` (per-user, local, NOT tracked
    in the beamline-agent repo). The user backs this file up out of
    band — the notes are personal knowledge, not something to push to
    a shared source tree. If a note is broadly useful, a human
    reviewer promotes it into a curated `context_docs/*.md` file.

    `tool` — short slug for what the note pertains to ("tomogui",
    "bl_gui", "conda", "ssh", "general"). Used to group entries when
    a human eventually promotes them into the curated per-tool docs.

    Never call this to remember what the user already told you within
    the same turn — history persistence handles that. Call ONLY for
    findings you'd want yourself to know at the START of a fresh turn
    that has no chat history."""
    if not topic or not topic.strip():
        return {"error": "topic is required"}
    if not content or not content.strip():
        return {"error": "content is required"}
    path = LEARNED_NOTES_PATH
    ts = datetime.now().isoformat(timespec="seconds")
    entry = (f"\n## [{tool}] {topic.strip()}   ({ts})\n\n"
             f"{content.strip()}\n\n---\n")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # If the file is new, add a friendly header so a human opening
        # it for the first time knows what they're looking at.
        is_new = not os.path.isfile(path)
        with open(path, "a") as f:
            if is_new:
                f.write("# Agent-learned notes\n\n"
                        "Auto-appended by the AI agent via "
                        "`save_learned_note`. Local to this user "
                        "(not in git). Review, promote broadly-"
                        "useful entries into a curated doc, then "
                        "delete the entry from here.\n\n")
            f.write(entry)
        return {"ok": True, "path": path, "bytes_added": len(entry),
                "deployment": "personal (~/.pystream/_learned.md — "
                              "not tracked in git; back up separately)"}
    except OSError as ex:
        return {"error": f"cannot write {path}: {ex}"}


def tool_read_task_recording(task_slug: str,
                              session_id: str = None) -> dict:
    """Return the full recorded action log for one task session.
    task_slug — subdirectory name (e.g. "zone_plate", "sample",
    "my_pinhole"). session_id — YYYYMMDD_HHMMSS folder; omit for the
    most recent session for that task. Returns:
        {"task": <display>, "session_id": ..., "session_dir": ...,
         "readme": "<README.md contents>",
         "actions": [<parsed jsonl rows>], "frame_count": N}
    or {"error": "..."} on failure."""
    try:
        task_dir = os.path.join(TASK_RECORDINGS_ROOT, task_slug)
        if not os.path.isdir(task_dir):
            return {"error": f"no task recordings for '{task_slug}'"}
        if session_id:
            sess_dir = os.path.join(task_dir, session_id)
            if not os.path.isdir(sess_dir):
                return {"error": f"session '{session_id}' not found for '{task_slug}'"}
        else:
            candidates = sorted(
                d for d in os.listdir(task_dir)
                if os.path.isdir(os.path.join(task_dir, d))
            )
            if not candidates:
                return {"error": f"no sessions recorded for '{task_slug}'"}
            session_id = candidates[-1]
            sess_dir = os.path.join(task_dir, session_id)
        rows = _read_jsonl(os.path.join(sess_dir, "actions.jsonl"))
        readme_path = os.path.join(sess_dir, "README.md")
        readme = ""
        if os.path.isfile(readme_path):
            try:
                with open(readme_path) as f:
                    readme = f.read()
            except OSError:
                pass
        frame_count = sum(
            1 for name in os.listdir(sess_dir)
            if name.endswith(".tif") or name.endswith(".tiff")
        )
        start = next((r for r in rows if r.get("type") == "session_start"), {})
        return {
            "task": start.get("element", task_slug),
            "task_slug": task_slug,
            "session_id": session_id,
            "session_dir": sess_dir,
            "readme": readme,
            "actions": rows,
            "frame_count": frame_count,
        }
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}


# ── catalog ─────────────────────────────────────────────────────────────

def _spawn_subagent_dispatch(kind: str, task: str) -> dict:
    """Late-bound wrapper: imports the impl at call time so we don't
    force a subagents-module import at package init."""
    from .subagents import tool_spawn_subagent
    return tool_spawn_subagent(kind, task)


def tool_list_beamline_plugins() -> dict:
    """List the plugins exposed by the currently-active beamline. Each
    entry: {class_name, button_text, group, handler_type, doc}. Use
    this before `open_beamline_plugin` when you're unsure whether a
    plugin exists or what its exact name is. Populated from the
    beamline's `__all__` list."""
    try:
        import json as _json
        from .subagents import WORKER_CTX
        from PyQt5 import QtCore
        gui_helper = getattr(WORKER_CTX, "gui_action", None)
        if gui_helper is None:
            return {"error": "GUI helper unavailable (tool called outside a chat turn?)"}
        payload = QtCore.QMetaObject.invokeMethod(
            gui_helper, "list_beamline_plugins_json",
            QtCore.Qt.BlockingQueuedConnection,
            QtCore.Q_RETURN_ARG(str),
        )
        parsed = _json.loads(payload) if payload else {}
        return parsed if isinstance(parsed, dict) else {"error": "invalid response"}
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}


def tool_open_beamline_plugin(name: str) -> dict:
    """Open a beamline plugin (dialog / launcher) that appears in the
    pystream toolbar. `name` matches the plugin's class name (e.g.
    `CenterOfRotationDialog`) OR its `BUTTON_TEXT` (e.g. `CoR`),
    case-insensitive. Returns after the dialog is on-screen. Use to
    fulfill user requests like "open CoR", "launch QGMax", "run TXM
    Optics"."""
    if not name or not name.strip():
        return {"error": "name is required"}
    try:
        from .subagents import WORKER_CTX
        from PyQt5 import QtCore
        gui_helper = getattr(WORKER_CTX, "gui_action", None)
        if gui_helper is None:
            return {"error": "GUI helper unavailable (tool called outside a chat turn?)"}
        err = QtCore.QMetaObject.invokeMethod(
            gui_helper, "open_beamline_plugin",
            QtCore.Qt.BlockingQueuedConnection,
            QtCore.Q_RETURN_ARG(str),
            QtCore.Q_ARG(str, name.strip()),
        )
        if err:
            # Return the FULL error verbatim (may include a traceback)
            # so the model surfaces the actual failure reason instead
            # of a bare "plugin didn't open". The user needs to see
            # missing-module errors etc. to know which env to fix.
            return {"error": err, "name": name,
                    "hint": ("If the error mentions a missing module "
                             "the plugin likely needs to run in a "
                             "different conda env — set `CONDA_ENV` "
                             "on the plugin class and use "
                             "pystream.launcher_utils.spawn_python_in_env().")}
        return {"ok": True, "name": name,
                "message": f"Opened plugin {name!r} — the user can now interact with the dialog. "
                           "For LAUNCHER-style plugins, the dialog closes immediately after "
                           "spawning the subprocess; the launched app is a separate window."}
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}", "name": name}


def tool_view_image(path: str) -> dict:
    """Open pystream's agent-only image viewer on a local file. Use
    for ANY image the agent produces or is handed by the user — PNGs
    of plots, TIFF slices scp'd from a remote host, matplotlib output,
    numpy `.npy` arrays. Handles grayscale + colour, single-frame +
    multi-page TIFF, 2D + 3D volumes."""
    if not path or not path.strip():
        return {"error": "path is required"}
    path = os.path.expanduser(path.strip())
    if not os.path.isfile(path):
        return {"error": f"file does not exist: {path}"}
    try:
        from .subagents import WORKER_CTX
        from PyQt5 import QtCore
        gui_helper = getattr(WORKER_CTX, "gui_action", None)
        if gui_helper is None:
            return {"error": "GUI helper unavailable (tool called outside a chat turn?)"}
        err = QtCore.QMetaObject.invokeMethod(
            gui_helper, "open_image_viewer",
            QtCore.Qt.BlockingQueuedConnection,
            QtCore.Q_RETURN_ARG(str),
            QtCore.Q_ARG(str, path),
        )
        if err:
            return {"error": err, "path": path}
        return {
            "ok": True,
            "path": path,
            "message": (f"Image viewer opened on {path}. "
                        f"User can zoom / pan / adjust levels."),
        }
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}", "path": path}


def tool_view_hdf5_file(path: str) -> dict:
    """Open pystream's embedded HDF5 viewer on a file on disk.
    Auto-detects raw-tomo vs reconstruction layout; for reconstruction
    files (tomogui/tomocupy `_rec.h5`, plain 3D stacks) the viewer
    shows slices directly. Returns after the dialog is on-screen —
    the viewer stays open for the user to interact with; this tool
    doesn't wait for them to close it."""
    if not path or not path.strip():
        return {"error": "path is required"}
    path = os.path.expanduser(path.strip())
    if not os.path.isfile(path):
        return {"error": f"file does not exist: {path}"}
    try:
        from .subagents import WORKER_CTX
        gui_helper = getattr(WORKER_CTX, "gui_action", None)
        if gui_helper is None:
            return {"error": "GUI helper unavailable — was this tool "
                             "called outside a pystream chat turn?"}
        # Marshal onto the GUI thread. Blocking so we return only after
        # the viewer is actually up (or has reported an error).
        from PyQt5 import QtCore
        result = QtCore.QMetaObject.invokeMethod(
            gui_helper, "open_hdf5_viewer",
            QtCore.Qt.BlockingQueuedConnection,
            QtCore.Q_RETURN_ARG(str),
            QtCore.Q_ARG(str, path),
        )
        if result:
            return {"error": result, "path": path}
        return {
            "ok": True,
            "path": path,
            "message": (f"HDF5 viewer opened on {path}. The user can now "
                        f"scroll slices, adjust contrast, view metadata."),
        }
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}", "path": path}


CORE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_beamline_plugins",
        "description": (
            "Enumerate every plugin the currently-active beamline "
            "exposes (CoR, AlignPart, QGMax, TXM Optics, XANES GUIs, "
            "aTomo, DataMap, etc. on bl32ID). Returns class names, "
            "button labels, groups, handler types, and one-line docs. "
            "Use before `open_beamline_plugin` when you don't remember "
            "the exact name — or to answer 'what plugins are available' "
            "questions without hallucinating."
        ),
        "schema": {"type": "object", "properties": {}, "required": []},
        "func": tool_list_beamline_plugins,
    },
    {
        "name": "open_beamline_plugin",
        "description": (
            "Open a beamline plugin dialog (or launcher) by class name "
            "or button text. Case-insensitive match. Examples: "
            "'CoR', 'CenterOfRotationDialog', 'QGMax', 'AlignPart', "
            "'TXM Optics', 'aTomo'. Fulfills user asks like 'open CoR', "
            "'launch QGMax', 'run TXM Optics'. Returns after the dialog "
            "is on-screen — the user drives the dialog directly from "
            "there. If the user names a plugin that doesn't exist on "
            "the active beamline, the error tells you the available "
            "set — quote that back to them; do NOT guess."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Class name (e.g. 'QGMaxDialog') "
                         "or button text (e.g. 'QGMax'). Match is "
                         "case-insensitive."},
            },
            "required": ["name"],
        },
        "func": tool_open_beamline_plugin,
    },
    {
        "name": "view_image",
        "description": (
            "Open pystream's built-in image viewer (agent-only — NOT "
            "on the toolbar) on a local file. Use for ANY image the "
            "agent produces or is handed: PNG plots, TIFF slices, "
            "matplotlib output, numpy .npy arrays, JPGs. Handles "
            "grayscale + colour, single-frame + multi-page TIFF, "
            "2D + 3D volumes (slider auto-appears for stacks). "
            "\n\n"
            "Prefer this over `view_hdf5_file` for non-HDF5 images. "
            "Prefer this over telling the user to `xdg-open` the "
            "file. If the file is on a REMOTE host, scp it to the "
            "local FS first (or use `tomogui-cli view ... --out -` "
            "streamed into a local .png). File must exist on the "
            "machine pystream is running on."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Absolute or ~/-relative path to "
                         "the image file. Extension is auto-detected: "
                         ".png / .jpg / .bmp / .gif / .tif / .tiff / .npy "
                         "and anything PIL can decode."},
            },
            "required": ["path"],
        },
        "func": tool_view_image,
    },
    {
        "name": "view_hdf5_file",
        "description": (
            "Open pystream's embedded HDF5 viewer on a file path. Use "
            "this whenever the user asks you to 'show' / 'view' / "
            "'display' / 'look at' an HDF5 file — reconstruction "
            "output (`_rec.h5`), a source projection stack, or any "
            "3D HDF5 volume. The viewer auto-detects raw-tomo layout "
            "(exchange/data + exchange/data_white) vs reconstruction "
            "layout (exchange/data alone, or exchange/recon, or plain "
            "/data) and swaps modes accordingly. Returns immediately "
            "after the dialog appears; the user interacts with it "
            "directly. Do NOT combine with `bash python -c ...` slice "
            "extraction — this tool IS your slice viewer."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Absolute or ~/-relative path to "
                         "the .h5 / .hdf5 file. Must exist on the LOCAL "
                         "filesystem the pystream GUI is running on — for "
                         "files on a remote host, scp/rsync them here "
                         "first (or use the tomogui-cli view --out - "
                         "pipe pattern documented in tomogui.md)."},
            },
            "required": ["path"],
        },
        "func": tool_view_hdf5_file,
    },
    {
        "name": "spawn_subagent",
        "description": (
            "Delegate a specialized task to a purpose-built sub-agent "
            "with its own system prompt + narrow tool set + fresh chat "
            "context. Use this whenever the user's ask maps onto a "
            "documented specialty (tomographic reconstruction via "
            "tomogui, and — as they're added — sample alignment, XANES "
            "setup, etc.). YOU are the orchestrator; you don't do the "
            "specialist work yourself. Available `kind` values live in "
            "`~/.pystream/docs/*.md` — currently 'reconstruction' for "
            "tomogui-cli work. Runs synchronously and returns the sub-"
            "agent's final summary; that summary is what you present "
            "back to the user. If the sub-agent errors out or hits its "
            "iteration cap, its error string comes back in the result "
            "— quote it to the user and STOP; do NOT try to redo the "
            "work yourself."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": "Sub-agent preset name — "
                         "e.g. 'reconstruction'. Determines the system "
                         "prompt + tool set."},
                "task": {"type": "string",
                         "description": "Full task description from the "
                         "user (or your reformulation of it). Pass "
                         "concrete details: machine, host, file paths, "
                         "GPU number, whether AI COR is wanted, etc. "
                         "The sub-agent has NO access to your chat "
                         "history — everything it needs must be in here."},
            },
            "required": ["kind", "task"],
        },
        "func": _spawn_subagent_dispatch,
    },
    {
        "name": "save_learned_note",
        "description": (
            "Append a durable note to the agent's own knowledge file "
            "for future turns. Call when you discover something worth "
            "remembering across sessions: a new CLI flag, a corrected "
            "path, a shell quirk on a specific machine, a failure "
            "workaround, or any insight about a tool that isn't "
            "already in `~/.pystream/docs/<tool>.md`. "
            "Writes to `~/.pystream/_learned.md` — per-user, local, "
            "NOT tracked in git. The user backs it up separately."
            "\n\n"
            "DO NOT call for things the user already told you this "
            "turn — chat history handles that. Call ONLY for findings "
            "you'd want yourself to know when starting a FRESH turn "
            "with no context. Skip trivia."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string",
                          "description": "Short title, e.g. "
                          "'tcsh login shell on tomo2 needs bash -lc'"},
                "content": {"type": "string",
                            "description": "The note body — markdown, "
                            "multi-line ok. Explain WHY, not just what."},
                "tool": {"type": "string",
                         "description": "Short slug for grouping "
                         "('tomogui', 'bl_gui', 'ssh', 'conda', 'general'). "
                         "Default 'general'."},
            },
            "required": ["topic", "content"],
        },
        "func": tool_save_learned_note,
    },
    {
        "name": "list_task_recordings",
        "description": (
            "Enumerate recorded beamline-task demonstrations the "
            "scientist has captured with pystream's Task Recorder — "
            "alignment procedures, sample positioning routines, scan "
            "setup steps, or any other repeatable motor-driven task. "
            "Returns task slugs, session counts, timestamps, and any "
            "user notes. Call this FIRST whenever the user asks how to "
            "perform any repeatable beamline procedure — the recording "
            "is the ground truth for how the scientist actually does it "
            "on this beamline, more reliable than reasoning from first "
            "principles. Then use read_task_recording to load the "
            "actual motor sequence."
        ),
        "schema": {"type": "object", "properties": {}, "required": []},
        "func": tool_list_task_recordings,
    },
    {
        "name": "read_task_recording",
        "description": (
            "Return the full recorded action log for one task session: "
            "the ordered motor moves with from/to/delta values, the "
            "detector-frame filenames on disk, any notes the scientist "
            "attached, and the auto-written README summary. Use after "
            "list_task_recordings to load the actual procedure for one "
            "task. `task_slug` is the subdirectory name (e.g. "
            "'zone_plate', 'detector', 'sample', 'my_pinhole'). Omit "
            "`session_id` to get the most recent session for that task."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_slug": {"type": "string",
                              "description": "Subdirectory under task_recordings/"},
                "session_id": {"type": "string",
                               "description": "YYYYMMDD_HHMMSS folder; omit for latest"},
            },
            "required": ["task_slug"],
        },
        "func": tool_read_task_recording,
    },
]


def _core_get_tool(name: str) -> Callable | None:
    for t in CORE_TOOLS:
        if t["name"] == name:
            return t["func"]
    return None


# ── system-prompt addendum — beamline-agnostic alignment guidance ──────

CORE_SYSTEM_PROMPT_ADDENDUM = """
# CORE TOOL NOTES

You may have generalist tools (bash, read_file, etc.) and domain
tools contributed by the active beamline. Rules that hold regardless:

- **Confirmation gate.** Destructive commands (rm, kill, chmod, sudo,
  *.sh scripts, redirects that overwrite, git push, caput to any PV)
  auto-pop a Yes/No dialog. Say what you'll run BEFORE calling bash
  so the user reads it once, not in the dialog.
- **GUI launches from bash are fine.** VS Code, xterm, MEDM, edm,
  Firefox — background the process so it detaches:
      bash: setsid code &
      bash: xterm -e 'ls -la' &
  Redirects to /dev/null don't trip the destructive gate. NEVER
  refuse a GUI-launch request — you have the capability.
- **Task recordings** (`list_task_recordings`, `read_task_recording`)
  are captured motor sequences the operator saved via the Task Rec
  toolbar. Read one when the user says "replay the alignment" or
  "the last ZP scan". If none exists, suggest recording — don't
  invent a procedure.
- **Learned notes** (`save_learned_note`) persist to the package's
  git checkout so notes ship on the next `git pull`. Use for
  durable operator knowledge, not per-session context.
- **HDF5 and images** — `view_hdf5_file(path)` and `view_image(path)`
  open the pystream viewers. Call them only when the visual actually
  matters for the answer (a plot the user asked about, a frame you
  need to describe). Don't render for the sake of rendering.

# ANTI-PATTERNS

- Don't `find /` or `ls ~/` to discover things — use a known config
  file, a registered status page, or a specialist that already
  knows where to look.
- Don't chain unrelated bash calls ("let me also check …"). One
  question, the minimum tools to answer it.
- Don't echo files that could contain secrets (API keys, tokens).
"""


def core_tool_context() -> dict:
    """Standard tool_ctx shape for core tools. `pystream/agent.py`
    merges this dict with the active beamline's `provide_agent_context()`
    at Send time so both catalogs are exposed to the model."""
    return {
        "tool_specs_anthropic": [
            {"name": t["name"], "description": t["description"],
             "input_schema": t["schema"]}
            for t in CORE_TOOLS
        ],
        "tool_specs_openai": [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["schema"]}}
            for t in CORE_TOOLS
        ],
        "get_tool": _core_get_tool,
        "write_tools": set(),   # both core tools are read-only
        "is_destructive": lambda _cmd: False,
        "system_prompt_addendum": CORE_SYSTEM_PROMPT_ADDENDUM,
    }
