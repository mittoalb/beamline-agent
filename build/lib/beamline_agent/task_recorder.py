"""Task recorder — captures motor moves and detector frames as a
timestamped log so a user can teach the AI agent (or themselves) how
to perform any repeatable beamline task. Alignment is the driving use
case but nothing here is alignment-specific; sample positioning, scan
setup, or any procedure with a defined motor set is fair game.

Beamline-agnostic core. The dialog is registered from
pystream/pystream.py's top toolbar (like the AI panel and HDF5 viewer);
the task/motor list comes from the active beamline's
`provide_task_templates()` hook when present, otherwise the dialog
falls back to a free-text task name + manual motor table.

Recorded sessions land under
    ~/.pystream/task_recordings/<task_slug>/<YYYYMMDD_HHMMSS>/
one directory per demonstration, containing:
    actions.jsonl               — append-only event log
    frame_0000_start.tif        — detector frame at session_start
    frame_NNNN.tif              — frame after each move-burst settles
    README.md                   — summary written on stop()

Published "tools" (blessed sessions promoted for one-click replay)
live in ~/.pystream/task_tools.json; per-task context strings that
auto-fill the dialog's Context editor live in
~/.pystream/task_contexts.json. Both files are migrated on first
import from their pre-rename names (alignment_*.json /
alignment_examples/).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

# Host state directory. beamline-agent doesn't inherit pystream's
# PYSTREAM_HOME — it stands on its own. Kept name-compatible with the
# old pystream layout so recordings created before the extraction
# still live at the same paths.
PYSTREAM_HOME = os.path.expanduser("~/.pystream")

TASK_RECORDINGS_ROOT = os.path.join(PYSTREAM_HOME, "task_recordings")
TASK_CONTEXTS_FILE  = os.path.join(PYSTREAM_HOME, "task_contexts.json")
TASK_TOOLS_FILE     = os.path.join(PYSTREAM_HOME, "task_tools.json")

# One-time migration from the pre-rename layout (alignment_examples/,
# alignment_contexts.json, alignment_tools.json). Runs at import; only
# moves each path if the NEW name doesn't already exist. Idempotent.
_LEGACY_PATHS: tuple = (
    (os.path.join(PYSTREAM_HOME, "alignment_examples"),      TASK_RECORDINGS_ROOT),
    (os.path.join(PYSTREAM_HOME, "alignment_contexts.json"), TASK_CONTEXTS_FILE),
    (os.path.join(PYSTREAM_HOME, "alignment_tools.json"),    TASK_TOOLS_FILE),
)


def _migrate_legacy_task_paths() -> None:
    for old, new in _LEGACY_PATHS:
        try:
            if os.path.exists(old) and not os.path.exists(new):
                os.makedirs(os.path.dirname(new), exist_ok=True)
                os.rename(old, new)
        except OSError:
            pass


_migrate_legacy_task_paths()

DEFAULT_MOVE_THRESHOLD = 1e-4    # ignore RBV chatter below this delta
DEFAULT_BURST_MS       = 400     # settle window after last RBV update
LIVE_LOG_MAX_ROWS      = 40
CONTEXT_AUTOSAVE_MS    = 600     # debounce for context editor -> disk

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers

def _task_slug(name: str) -> str:
    """'Zone Plate' -> 'zone_plate'; strips punctuation, collapses runs."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return s or "unnamed"


def _active_task_templates() -> Dict[str, dict]:
    """Query the active beamline's provide_task_templates() hook.
    Normalizes the return shape so each value is a dict:
        {"motors": [{"label","pv"}, ...], "context": <str>}

    The hook may return either the new shape (dict) or the legacy shape
    (bare list of motor_specs). Both are accepted for back-compat.
    Returns {} if no beamline active or the hook is absent."""
    mod = None
    # 1. Ask the host for its active beamline module, if any.
    try:
        from . import get_active_host
        host = get_active_host()
    except Exception:
        host = None
    if host is not None and host.beamline_lookup and host.active_beamline:
        try:
            mod = host.beamline_lookup(host.active_beamline)
        except Exception:
            mod = None
    # 2. Fall back to pystream — the primary host today.
    if mod is None:
        try:
            from pystream.beamline_config import ACTIVE_BEAMLINE
            if not ACTIVE_BEAMLINE:
                return {}
            mod = importlib.import_module(
                f".beamlines.{ACTIVE_BEAMLINE}", package="pystream")
        except Exception:
            return {}
    if mod is None:
        return {}
    hook = getattr(mod, "provide_task_templates", None)
    if hook is None:
        return {}
    try:
        raw = hook() or {}
    except Exception as e:
        LOGGER.warning("provide_task_templates failed: %s", e)
        return {}
    out: Dict[str, dict] = {}
    for name, val in raw.items():
        if isinstance(val, list):
            out[name] = {"motors": val, "context": ""}
        elif isinstance(val, dict):
            out[name] = {
                "motors":  list(val.get("motors") or []),
                "context": str(val.get("context") or ""),
            }
    return out


def _load_contexts() -> Dict[str, str]:
    """User-edited context strings keyed by element name. Overrides the
    curated defaults from provide_task_templates(). Missing file or
    parse error → empty dict (defaults apply)."""
    try:
        with open(TASK_CONTEXTS_FILE) as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_contexts(contexts: Dict[str, str]) -> None:
    """Persist the {element_name: context_text} map. Written atomically
    via tempfile+rename so a crash mid-write can't corrupt the file."""
    try:
        os.makedirs(os.path.dirname(TASK_CONTEXTS_FILE), exist_ok=True)
        tmp = TASK_CONTEXTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(contexts, f, indent=2, sort_keys=True)
        os.replace(tmp, TASK_CONTEXTS_FILE)
    except OSError as e:
        LOGGER.warning("failed to write %s: %s", TASK_CONTEXTS_FILE, e)


# ── published tools store ────────────────────────────────────────────
#
# A "published tool" is a named, blessed task recording that the user
# has committed to for one-click replay. Multiple recordings under
# `task_recordings/<slug>/` may exist during training; the user picks
# one and calls it e.g. "Zone Plate" — the tool is a pointer to that
# session dir plus a snapshot of its motor list. Republishing under the
# same name overwrites: exactly what the user wants when a PV changes
# or hardware is reconfigured and they re-train.
#
# Schema of TASK_TOOLS_FILE:
#     {
#       "<tool_name>": {
#         "element": "Zone Plate",           # display name recorded at Start
#         "element_slug": "zone_plate",      # dir under task_recordings/
#         "session_id":  "20260819_142530",  # subdir under element_slug/
#         "session_dir": "/abs/path/…",      # resolved path for portability
#         "motors":      [{"label","pv"}, …],# snapshot from session_start
#         "n_moves":     12,
#         "description": "…",                # user note (optional)
#         "published_ts": 1697…,
#       }, …
#     }


def _load_tools() -> Dict[str, dict]:
    try:
        with open(TASK_TOOLS_FILE) as f:
            data = json.load(f)
        return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_tools(tools: Dict[str, dict]) -> None:
    try:
        os.makedirs(os.path.dirname(TASK_TOOLS_FILE), exist_ok=True)
        tmp = TASK_TOOLS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(tools, f, indent=2, sort_keys=True)
        os.replace(tmp, TASK_TOOLS_FILE)
    except OSError as e:
        LOGGER.warning("failed to write %s: %s", TASK_TOOLS_FILE, e)


def _tool_from_session(session_dir: str, description: str = "") -> dict:
    """Build a tool record from a session directory (reads its jsonl to
    extract element, motors, move count). Raises ValueError if the
    session dir isn't a valid task recording."""
    jsonl = os.path.join(session_dir, "actions.jsonl")
    if not os.path.isfile(jsonl):
        raise ValueError(f"no actions.jsonl in {session_dir}")
    element = ""
    motors: list = []
    n_moves = 0
    with open(jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = row.get("type")
            if t == "session_start":
                element = str(row.get("element") or "")
                motors = list(row.get("motors_monitored") or [])
            elif t == "motor_move":
                n_moves += 1
    if n_moves == 0:
        raise ValueError(f"{session_dir}: no motor_move events (nothing to replay)")
    slug = os.path.basename(os.path.dirname(session_dir))
    session_id = os.path.basename(session_dir)
    return {
        "element":      element or slug.replace("_", " ").title(),
        "element_slug": slug,
        "session_id":   session_id,
        "session_dir":  session_dir,
        "motors":       motors,
        "n_moves":      n_moves,
        "description":  description,
        "published_ts": time.time(),
    }


def _write_tiff(path: str, img: np.ndarray) -> None:
    """Write frame to TIFF. Prefer tifffile (fast + preserves dtype);
    fall back to PIL. Silently no-ops if neither installed and image
    is unwriteable."""
    try:
        import tifffile
        tifffile.imwrite(path, np.ascontiguousarray(img))
        return
    except ImportError:
        pass
    try:
        from PIL import Image
        arr = img
        if arr.dtype not in (np.uint8, np.uint16, np.float32):
            arr = arr.astype(np.float32)
        Image.fromarray(arr).save(path)
    except Exception as e:
        LOGGER.warning("failed to write TIFF %s: %s", path, e)


# ------------------------------------------------------------- engine

class TaskRecorder(QtCore.QObject):
    """Records motor moves + detector frames into a session directory.

    Subscribes to `<motor_pv>.RBV` via `epics.PV(auto_monitor=True,
    callback=…)`. The callback runs in a CA background thread, so
    a `_rbv_signal` re-emits into the GUI thread where the debounce
    timer and file I/O live.
    """

    event_recorded = QtCore.pyqtSignal(dict)    # for the live-log UI
    _rbv_signal    = QtCore.pyqtSignal(str, float, float)  # pv, value, ts

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pvs: Dict[str, object] = {}         # pv_str -> epics.PV
        self._motor_by_pv: Dict[str, Dict] = {}   # pv_str -> {"label","pv"}
        self._grab_frame: Optional[Callable[[], Optional[np.ndarray]]] = None
        self._session_dir: Optional[str] = None
        self._element: Optional[str] = None
        self._task_slug: Optional[str] = None
        self._session_started: Optional[float] = None
        self._motors_monitored: List[Dict[str, str]] = []
        self._move_threshold = DEFAULT_MOVE_THRESHOLD
        self._burst_ms = DEFAULT_BURST_MS
        self._n_moves = 0
        self._n_frames = 0

        # Burst-tracking state (guarded by _lock — only touched from GUI
        # thread via the signal slot).
        self._lock = threading.Lock()
        self._burst_active = False
        self._burst_moves: Dict[str, Dict[str, float]] = {}
        self._burst_started: float = 0.0
        self._prev_rbv: Dict[str, float] = {}
        self._burst_timer = QtCore.QTimer(self)
        self._burst_timer.setSingleShot(True)
        self._burst_timer.timeout.connect(self._on_burst_settled)

        self._rbv_signal.connect(self._on_rbv_gui_thread,
                                 QtCore.Qt.QueuedConnection)

    # ------------------------------------------------------- public

    def is_recording(self) -> bool:
        return self._session_dir is not None

    @property
    def session_dir(self) -> Optional[str]:
        return self._session_dir

    def start(self,
              element: str,
              motor_specs: List[Dict[str, str]],
              grab_frame_fn: Callable[[], Optional[np.ndarray]],
              opening_note: str = "",
              move_threshold: float = DEFAULT_MOVE_THRESHOLD,
              burst_ms: int = DEFAULT_BURST_MS) -> str:
        if self.is_recording():
            raise RuntimeError("recorder is already recording; stop first")
        if not element:
            raise ValueError("element name is required")
        if not motor_specs:
            raise ValueError("at least one motor is required")

        import epics
        self._element = element
        self._task_slug = _task_slug(element)
        self._grab_frame = grab_frame_fn
        self._move_threshold = float(move_threshold)
        self._burst_ms = int(burst_ms)
        self._motors_monitored = [dict(m) for m in motor_specs]
        self._n_moves = 0
        self._n_frames = 0
        self._motor_by_pv = {m["pv"]: m for m in self._motors_monitored}

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_dir = os.path.join(
            TASK_RECORDINGS_ROOT, self._task_slug, ts)
        os.makedirs(self._session_dir, exist_ok=True)
        self._session_started = time.time()

        # Prime prev_rbv with a single caget so the first real callback
        # doesn't spuriously look like a huge jump from 0.
        for m in self._motors_monitored:
            rbv_pv = m["pv"] + ".RBV"
            try:
                v = epics.caget(rbv_pv, timeout=1.0)
                self._prev_rbv[m["pv"]] = float(v) if v is not None else 0.0
            except Exception:
                self._prev_rbv[m["pv"]] = 0.0

        # Session header — plus one frame capturing the initial state
        # (agent's baseline for reasoning about what changed).
        start_frame = self._save_frame("frame_0000_start.tif")
        self._append_event({
            "type": "session_start",
            "element": element,
            "motors_monitored": self._motors_monitored,
            "opening_note": opening_note,
            "move_threshold": self._move_threshold,
            "burst_ms": self._burst_ms,
            "start_frame": start_frame,
        })

        # Subscribe. Capture the motor pv (not .RBV) in the closure so
        # our state maps stay consistent.
        for m in self._motors_monitored:
            motor_pv = m["pv"]
            rbv_pv = motor_pv + ".RBV"
            try:
                pv = epics.PV(rbv_pv, auto_monitor=True)
                pv.add_callback(self._make_rbv_cb(motor_pv))
                self._pvs[motor_pv] = pv
            except Exception as e:
                LOGGER.warning("failed to subscribe to %s: %s", rbv_pv, e)

        return self._session_dir

    def stop(self, closing_note: str = "") -> None:
        if not self.is_recording():
            return

        # If a burst is in flight, flush it before tearing down.
        if self._burst_active:
            self._burst_timer.stop()
            self._on_burst_settled()

        for pv in self._pvs.values():
            try:
                pv.clear_callbacks()
                pv.disconnect()
            except Exception:
                pass
        self._pvs.clear()
        self._motor_by_pv.clear()

        elapsed = time.time() - (self._session_started or time.time())
        self._append_event({
            "type": "session_end",
            "closing_note": closing_note,
            "elapsed_s": round(elapsed, 3),
            "moves": self._n_moves,
            "frames": self._n_frames,
        })
        self._write_readme(closing_note, elapsed)

        self._session_dir = None
        self._element = None
        self._task_slug = None
        self._session_started = None
        self._motors_monitored = []
        self._prev_rbv.clear()
        self._burst_active = False
        self._burst_moves.clear()

    def snapshot(self, tag: str = "manual") -> None:
        if not self.is_recording():
            return
        fname = self._save_frame(f"snap_{self._n_frames + 1:04d}.tif")
        if fname:
            self._append_event({
                "type": "snapshot",
                "tag": tag,
                "frame": fname,
            })

    def add_note(self, text: str) -> None:
        if not self.is_recording() or not text:
            return
        self._append_event({"type": "note", "text": text})

    # ---------------------------------------------------- internals

    def _make_rbv_cb(self, motor_pv: str) -> Callable:
        # pyepics callback signature — do minimal work here, hand off
        # to the GUI thread via signal.
        def _cb(pvname=None, value=None, timestamp=None, **_kw):
            if value is None:
                return
            try:
                self._rbv_signal.emit(motor_pv, float(value),
                                      float(timestamp or time.time()))
            except (TypeError, ValueError):
                pass
        return _cb

    @QtCore.pyqtSlot(str, float, float)
    def _on_rbv_gui_thread(self, motor_pv: str, value: float,
                           timestamp: float) -> None:
        if not self.is_recording():
            return
        prev = self._prev_rbv.get(motor_pv, value)
        delta = value - prev
        if abs(delta) < self._move_threshold:
            return

        if not self._burst_active:
            self._burst_active = True
            self._burst_started = timestamp
            self._burst_moves = {}

        rec = self._burst_moves.setdefault(motor_pv, {
            "pv": motor_pv,
            "label": self._motor_by_pv.get(motor_pv, {}).get("label", motor_pv),
            "from": prev,
            "to": value,
        })
        rec["to"] = value  # latest value each callback
        self._prev_rbv[motor_pv] = value

        # (Re)start the settle timer.
        self._burst_timer.start(self._burst_ms)

    def _on_burst_settled(self) -> None:
        if not self._burst_active:
            return
        self._burst_active = False
        motors = []
        for rec in self._burst_moves.values():
            rec["delta"] = rec["to"] - rec["from"]
            motors.append(rec)
        self._burst_moves = {}
        # One frame per move — after the burst settles. No before-frame
        # (the prior move's after-frame IS the current move's before, so
        # capturing before-frames would double storage for no new info).
        frame = self._save_frame(f"frame_{self._n_moves + 1:04d}.tif")
        duration = time.time() - self._burst_started
        self._n_moves += 1
        self._append_event({
            "type": "motor_move",
            "motors": motors,
            "frame": frame,
            "duration_s": round(duration, 3),
        })

    def _save_frame(self, fname: str) -> Optional[str]:
        if self._session_dir is None or self._grab_frame is None:
            return None
        try:
            img = self._grab_frame()
        except Exception as e:
            LOGGER.warning("grab_frame_fn failed: %s", e)
            return None
        if img is None or not isinstance(img, np.ndarray):
            return None
        path = os.path.join(self._session_dir, fname)
        _write_tiff(path, img)
        if os.path.exists(path):
            self._n_frames += 1
            return fname
        return None

    def _append_event(self, event: dict) -> None:
        if self._session_dir is None:
            return
        event = dict(event)
        event["ts"] = event.get("ts") or time.time()
        line = json.dumps(event, default=str)
        path = os.path.join(self._session_dir, "actions.jsonl")
        try:
            with open(path, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            LOGGER.warning("failed to append event: %s", e)
        # UI notification
        try:
            self.event_recorded.emit(event)
        except Exception:
            pass

    def _write_readme(self, closing_note: str, elapsed: float) -> None:
        if self._session_dir is None:
            return
        started_iso = datetime.fromtimestamp(
            self._session_started or time.time()).isoformat(timespec="seconds")
        ended_iso = datetime.now().isoformat(timespec="seconds")
        motor_lines = "\n".join(
            f"- **{m['label']}** — `{m['pv']}`"
            for m in self._motors_monitored)
        note_block = (f"\n## Closing note\n\n{closing_note}\n"
                      if closing_note else "")
        body = f"""# Task session: {self._element}

- **Started**: {started_iso}
- **Ended**:   {ended_iso}
- **Elapsed**: {elapsed:.1f} s
- **Moves**:   {self._n_moves}
- **Frames**:  {self._n_frames}

## Motors monitored

{motor_lines}
{note_block}
Machine-readable log: `actions.jsonl`
"""
        try:
            with open(os.path.join(self._session_dir, "README.md"), "w") as f:
                f.write(body)
        except OSError as e:
            LOGGER.warning("failed to write README.md: %s", e)


# ------------------------------------------------------------- dialog

class TaskRecorderDialog(QtWidgets.QDialog):
    """User-facing dialog for the recorder. Owns one TaskRecorder."""

    BUTTON_TEXT  = "Task Rec"
    GROUP        = "Tools"
    HANDLER_TYPE = 'singleton'

    def __init__(self, parent=None, logger: Optional[logging.Logger] = None):
        super().__init__(parent)
        self.setWindowTitle("Task Recorder")
        self.setModal(False)
        self.resize(1100, 620)
        self.logger = logger or LOGGER

        self._recorder = TaskRecorder(self)
        self._recorder.event_recorded.connect(self._on_event)
        self._elements = _active_task_templates()
        self._contexts = _load_contexts()

        # Context-editor autosave debounce (writes TASK_CONTEXTS_FILE atomically).
        self._ctx_save_timer = QtCore.QTimer(self)
        self._ctx_save_timer.setSingleShot(True)
        self._ctx_save_timer.setInterval(CONTEXT_AUTOSAVE_MS)
        self._ctx_save_timer.timeout.connect(self._flush_context_save)
        self._ctx_dirty_key: Optional[str] = None

        self._build_ui()
        self._reload_element_dropdown()
        self._update_button_state()

    # ------------------------------------------------------- UI

    def _build_ui(self) -> None:
        root = QtWidgets.QHBoxLayout(self)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self)
        root.addWidget(split, 1)

        # ─── LEFT PANE ────────────────────────────────────────────────
        left = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(left)
        v.setContentsMargins(0, 0, 8, 0)

        # Element selector
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Task:"))
        self._element_combo = QtWidgets.QComboBox()
        self._element_combo.currentTextChanged.connect(self._on_element_changed)
        row.addWidget(self._element_combo, 1)
        v.addLayout(row)

        # Free-text override (visible only when no task templates from
        # the active beamline, or the user picks "— free-text —")
        self._freetext_group = QtWidgets.QGroupBox("Free-text task")
        ft_layout = QtWidgets.QVBoxLayout(self._freetext_group)
        self._freetext_name = QtWidgets.QLineEdit()
        self._freetext_name.setPlaceholderText(
            "Task name (e.g. Sample alignment, Zone plate focus, …)")
        self._freetext_name.textChanged.connect(self._on_freetext_name_changed)
        ft_layout.addWidget(self._freetext_name)
        self._motor_table = QtWidgets.QTableWidget(0, 2)
        self._motor_table.setHorizontalHeaderLabels(["Label", "Motor PV"])
        self._motor_table.horizontalHeader().setStretchLastSection(True)
        self._motor_table.itemChanged.connect(self._update_button_state)
        ft_layout.addWidget(self._motor_table)
        row_btn = QtWidgets.QHBoxLayout()
        add_row = QtWidgets.QPushButton("+ motor")
        add_row.clicked.connect(lambda: self._motor_table.insertRow(
            self._motor_table.rowCount()))
        del_row = QtWidgets.QPushButton("− motor")
        del_row.clicked.connect(self._remove_selected_motor_row)
        row_btn.addWidget(add_row)
        row_btn.addWidget(del_row)
        row_btn.addStretch(1)
        ft_layout.addLayout(row_btn)
        v.addWidget(self._freetext_group)

        # Motor preview + checkbox filter
        v.addWidget(QtWidgets.QLabel("Monitored motors (uncheck to skip):"))
        self._motor_list = QtWidgets.QListWidget()
        self._motor_list.setSelectionMode(
            QtWidgets.QAbstractItemView.NoSelection)
        v.addWidget(self._motor_list, 1)

        # Control buttons
        row = QtWidgets.QHBoxLayout()
        self._start_btn = QtWidgets.QPushButton("● Start recording")
        self._start_btn.clicked.connect(self._on_start_stop)
        self._snap_btn = QtWidgets.QPushButton("📸 Snapshot")
        self._snap_btn.clicked.connect(self._on_snapshot)
        self._note_btn = QtWidgets.QPushButton("📝 Add note")
        self._note_btn.clicked.connect(self._on_add_note)
        self._load_btn = QtWidgets.QPushButton("📂 Load")
        self._load_btn.setToolTip(
            "Browse recorded sessions and publish one as a tool")
        self._load_btn.clicked.connect(self._on_load)
        self._tools_btn = QtWidgets.QPushButton("🛠 Tools")
        self._tools_btn.setToolTip(
            "Open the list of published task tools — one-click "
            "replay of blessed recorded procedures")
        self._tools_btn.clicked.connect(self._on_open_tools)
        row.addWidget(self._start_btn)
        row.addWidget(self._snap_btn)
        row.addWidget(self._note_btn)
        row.addWidget(self._load_btn)
        row.addWidget(self._tools_btn)
        row.addStretch(1)
        v.addLayout(row)

        # Live event log
        v.addWidget(QtWidgets.QLabel("Live event log:"))
        self._log_list = QtWidgets.QListWidget()
        self._log_list.setStyleSheet("QListWidget { font-family: monospace; }")
        v.addWidget(self._log_list, 2)

        # Status
        self._status_label = QtWidgets.QLabel("not recording")
        self._status_label.setWordWrap(True)
        v.addWidget(self._status_label)

        split.addWidget(left)

        # ─── RIGHT PANE: Context editor ───────────────────────────────
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(8, 0, 0, 0)

        header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel("<b>Context</b>"))
        self._ctx_status = QtWidgets.QLabel("")
        self._ctx_status.setStyleSheet("color: #888; font-size: 11px;")
        header.addWidget(self._ctx_status)
        header.addStretch(1)
        self._reset_ctx_btn = QtWidgets.QPushButton("Reset to default")
        self._reset_ctx_btn.setToolTip(
            "Discard your edits for this element and restore the "
            "curated default context (if the beamline provides one).")
        self._reset_ctx_btn.clicked.connect(self._reset_current_context)
        header.addWidget(self._reset_ctx_btn)
        rv.addLayout(header)

        self._ctx_edit = QtWidgets.QPlainTextEdit()
        self._ctx_edit.setPlaceholderText(
            "Free-form notes on what this task involves, why, "
            "known pitfalls, what \"aligned\" looks like…\n\n"
            "Auto-saved per element to ~/.pystream/task_contexts.json. "
            "When recording, this text becomes the session's opening_note "
            "in actions.jsonl.")
        self._ctx_edit.setStyleSheet(
            "QPlainTextEdit { font-family: monospace; }")
        self._ctx_edit.textChanged.connect(self._on_context_edited)
        rv.addWidget(self._ctx_edit, 1)

        split.addWidget(right)
        split.setSizes([560, 540])

    def _remove_selected_motor_row(self) -> None:
        r = self._motor_table.currentRow()
        if r >= 0:
            self._motor_table.removeRow(r)
        self._update_button_state()

    def _reload_element_dropdown(self) -> None:
        self._element_combo.blockSignals(True)
        self._element_combo.clear()
        if self._elements:
            for name in self._elements:
                self._element_combo.addItem(name)
            self._element_combo.addItem("— free-text —")
        else:
            self._element_combo.addItem(
                "(no task templates for this beamline — free-text below)")
        self._element_combo.blockSignals(False)
        self._on_element_changed(self._element_combo.currentText())

    def _on_element_changed(self, name: str) -> None:
        # If the previous element had unsaved context edits pending in
        # the debounce timer, flush them BEFORE we switch keys.
        self._flush_context_save()

        entry = self._elements.get(name, {})
        motors = entry.get("motors", [])
        is_free = (not self._elements) or (name == "— free-text —")
        self._freetext_group.setVisible(is_free)
        self._motor_list.clear()
        if not is_free:
            for m in motors:
                item = QtWidgets.QListWidgetItem(f"{m['label']}  ({m['pv']})")
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.Checked)
                item.setData(QtCore.Qt.UserRole, m)
                self._motor_list.addItem(item)

        # Load context for the new element key (user override → curated
        # default → empty). Free-text mode uses the typed name as its
        # key; on first entry that's blank, so nothing loads.
        self._load_context_for_current_element()
        self._update_button_state()

    def _on_freetext_name_changed(self, _text: str) -> None:
        """Free-text element name typed → element key changed → reload
        context under the new key."""
        self._flush_context_save()
        self._load_context_for_current_element()
        self._update_button_state()

    # ------------------------------------------------------- state

    def _current_element_and_motors(self) -> Optional[tuple]:
        """Returns (element_name, motor_specs) or None if incomplete."""
        combo = self._element_combo.currentText()
        is_free = (not self._elements) or (combo == "— free-text —")
        if is_free:
            name = self._freetext_name.text().strip()
            if not name:
                return None
            motors = []
            for r in range(self._motor_table.rowCount()):
                lbl_item = self._motor_table.item(r, 0)
                pv_item = self._motor_table.item(r, 1)
                pv = pv_item.text().strip() if pv_item else ""
                if not pv:
                    continue
                lbl = lbl_item.text().strip() if lbl_item else ""
                motors.append({"label": lbl or pv, "pv": pv})
            if not motors:
                return None
            return name, motors
        # dropdown path
        motors = []
        for i in range(self._motor_list.count()):
            item = self._motor_list.item(i)
            if item.checkState() == QtCore.Qt.Checked:
                motors.append(dict(item.data(QtCore.Qt.UserRole)))
        if not motors:
            return None
        return combo, motors

    def _current_element_key(self) -> str:
        """Element name used as the key in `task_contexts.json` and
        as the display name recorded in `session_start`. Falls back to
        free-text name when in free-text mode."""
        combo = self._element_combo.currentText()
        is_free = (not self._elements) or (combo == "— free-text —")
        if is_free:
            return self._freetext_name.text().strip()
        return combo

    def _default_context_for(self, name: str) -> str:
        """Curated default context text from the beamline hook (empty
        string if free-text / absent element)."""
        return self._elements.get(name, {}).get("context", "")

    # ── context persistence (per-element, auto-saved) ───────────────

    def _load_context_for_current_element(self) -> None:
        """Populate the Context editor for the current element key:
        user override if present in the JSON, otherwise curated default.
        Blocks signals so this doesn't schedule a save of what we just
        loaded."""
        key = self._current_element_key()
        text = self._contexts.get(key) or self._default_context_for(key)
        blocker = QtCore.QSignalBlocker(self._ctx_edit)
        self._ctx_edit.setPlainText(text)
        del blocker
        self._refresh_ctx_status(key)

    def _on_context_edited(self) -> None:
        """Debounced write: user typed → arm the save timer."""
        key = self._current_element_key()
        if not key:
            self._refresh_ctx_status(key, dirty=True)
            return
        self._ctx_dirty_key = key
        self._contexts[key] = self._ctx_edit.toPlainText()
        self._ctx_save_timer.start()
        self._refresh_ctx_status(key, dirty=True)

    def _flush_context_save(self) -> None:
        """Timer fired (or explicit flush before switching elements /
        closing dialog): persist to disk if anything dirty."""
        if self._ctx_dirty_key is None:
            return
        self._ctx_save_timer.stop()
        _save_contexts(self._contexts)
        key = self._ctx_dirty_key
        self._ctx_dirty_key = None
        self._refresh_ctx_status(key)

    def _refresh_ctx_status(self, key: str, dirty: bool = False) -> None:
        if not key:
            self._ctx_status.setText("(pick a task or type a free-text name)")
            self._reset_ctx_btn.setEnabled(False)
            return
        default = self._default_context_for(key)
        overridden = key in self._contexts and self._contexts.get(key, "") != default
        parts = []
        if dirty:
            parts.append("unsaved…")
        elif overridden:
            parts.append("your version — auto-saved")
        elif default:
            parts.append("curated default (edit → auto-saved override)")
        else:
            parts.append("no default — auto-saved as you type")
        self._ctx_status.setText("  ".join(parts))
        self._reset_ctx_btn.setEnabled(bool(default) and overridden)

    def _reset_current_context(self) -> None:
        key = self._current_element_key()
        default = self._default_context_for(key)
        if not key or not default:
            return
        resp = QtWidgets.QMessageBox.question(
            self, "Reset context",
            f"Discard your edits for “{key}” and restore the curated default?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        self._contexts.pop(key, None)
        _save_contexts(self._contexts)
        blocker = QtCore.QSignalBlocker(self._ctx_edit)
        self._ctx_edit.setPlainText(default)
        del blocker
        self._refresh_ctx_status(key)

    def _update_button_state(self) -> None:
        recording = self._recorder.is_recording()
        can_start = self._current_element_and_motors() is not None
        self._start_btn.setEnabled(recording or can_start)
        self._start_btn.setText(
            "■ Stop recording" if recording else "● Start recording")
        self._snap_btn.setEnabled(recording)
        self._note_btn.setEnabled(recording)
        self._element_combo.setEnabled(not recording)
        self._motor_list.setEnabled(not recording)
        self._freetext_group.setEnabled(not recording)
        # Context editor stays editable during recording — the value
        # already committed to the session's opening_note is frozen at
        # Start time, but the user is free to edit for the next session.

    # ------------------------------------------------------- actions

    def _on_start_stop(self) -> None:
        if self._recorder.is_recording():
            note, ok = QtWidgets.QInputDialog.getText(
                self, "Closing note",
                "Optional closing note (blank to skip):")
            self._recorder.stop(closing_note=note.strip() if ok else "")
            self._status_label.setText(
                f"not recording — last session saved to "
                f"{self._recorder.session_dir or '(none)'}")
        else:
            pick = self._current_element_and_motors()
            if pick is None:
                QtWidgets.QMessageBox.warning(
                    self, "Task Recorder",
                    "Pick a task and at least one motor first.")
                return
            element, motors = pick
            grab_fn = self._make_frame_grabber()
            # Make sure any pending context edits are on disk before we
            # snapshot the opening_note into the session log.
            self._flush_context_save()
            try:
                sess = self._recorder.start(
                    element, motors, grab_fn,
                    opening_note=self._ctx_edit.toPlainText().strip())
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self, "Start failed", str(e))
                return
            self._log_list.clear()
            self._status_label.setText(f"recording — session {sess}")
        self._update_button_state()

    def _on_snapshot(self) -> None:
        tag, ok = QtWidgets.QInputDialog.getText(
            self, "Snapshot tag", "Optional tag:", text="good")
        self._recorder.snapshot(tag=tag.strip() if ok else "manual")

    def _on_add_note(self) -> None:
        text, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "Add note", "Note text:")
        if ok and text.strip():
            self._recorder.add_note(text.strip())

    def _on_load(self) -> None:
        """Open the browser dialog to inspect a past recording. Read-only —
        doesn't touch the live recorder."""
        if self._recorder.is_recording():
            QtWidgets.QMessageBox.information(
                self, "Task Recorder",
                "Stop the current recording before browsing past sessions.")
            return
        dlg = SessionBrowserDialog(self)
        dlg.exec_()

    def _on_open_tools(self) -> None:
        """Open the Published Tools dialog for one-click replay."""
        if self._recorder.is_recording():
            QtWidgets.QMessageBox.information(
                self, "Task Recorder",
                "Stop the current recording before running a published tool.")
            return
        dlg = PublishedToolsDialog(self)
        dlg.exec_()

    def _make_frame_grabber(self) -> Callable[[], Optional[np.ndarray]]:
        """Return a zero-arg callable that yields the currently displayed
        frame. Prefers HostContext.live_frame (the clean protocol); falls
        back to reaching into a pystream-style parent viewer."""
        try:
            from . import get_active_host
            host = get_active_host()
        except Exception:
            host = None
        if host is not None and callable(host.live_frame):
            live_frame = host.live_frame

            def _grab_via_host() -> Optional[np.ndarray]:
                try:
                    img = live_frame()
                    if img is None:
                        return None
                    return np.array(img, copy=True)
                except Exception:
                    return None
            return _grab_via_host

        parent = self.parent()

        def _grab_via_parent() -> Optional[np.ndarray]:
            try:
                item = parent.image_view.getImageItem()
                img = item.image
                if img is None:
                    return None
                return np.array(img, copy=True)
            except Exception:
                return None
        return _grab_via_parent

    # ------------------------------------------------------- events

    @QtCore.pyqtSlot(dict)
    def _on_event(self, event: dict) -> None:
        etype = event.get("type", "?")
        ts = datetime.fromtimestamp(event.get("ts", time.time())).strftime("%H:%M:%S")
        if etype == "motor_move":
            parts = [
                f"{m['label']} {m['from']:.4g} → {m['to']:.4g} "
                f"(Δ{m['delta']:+.4g})"
                for m in event.get("motors", [])
            ]
            line = f"{ts}  MOVE  " + "   ".join(parts)
        elif etype == "snapshot":
            line = f"{ts}  SNAP  tag={event.get('tag','')}"
        elif etype == "note":
            line = f"{ts}  NOTE  {event.get('text','')[:80]}"
        elif etype == "session_start":
            line = f"{ts}  START element={event.get('element','')}"
        elif etype == "session_end":
            line = (f"{ts}  END   "
                    f"moves={event.get('moves')} frames={event.get('frames')}")
        else:
            line = f"{ts}  {etype.upper()}  {event}"
        self._log_list.addItem(line)
        while self._log_list.count() > LIVE_LOG_MAX_ROWS:
            self._log_list.takeItem(0)
        self._log_list.scrollToBottom()

        if self._recorder.is_recording():
            self._status_label.setText(
                f"recording — {self._recorder._n_moves} moves, "
                f"{self._recorder._n_frames} frames, "
                f"session {self._recorder.session_dir}")

    def closeEvent(self, ev) -> None:
        # Flush any pending context edits so nothing typed is lost.
        self._flush_context_save()
        if self._recorder.is_recording():
            resp = QtWidgets.QMessageBox.question(
                self, "Recording in progress",
                "A recording is active. Stop it before closing?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            if resp == QtWidgets.QMessageBox.Yes:
                self._recorder.stop()
        super().closeEvent(ev)


# ────────────────────────────────────────────────────────────── replay

class ReplayWorker(QtCore.QThread):
    """Replays a recorded session's motor_move events by driving
    `caput -c` (blocks until DMOV) on each motor. Emits progress
    signals for the UI.

    Each `motor_move` row in the JSONL becomes one step. Motors that
    changed together in one recorded burst are commanded concurrently
    from within the step (all caput -c calls run in a small thread
    pool inside that step) and the step advances only when they've
    all settled.
    """

    progress = QtCore.pyqtSignal(int, int, str)   # (i, total, message)
    step_ok  = QtCore.pyqtSignal(int, dict)       # (i, event)
    step_err = QtCore.pyqtSignal(int, dict, str)  # (i, event, error)
    finished_ok = QtCore.pyqtSignal(int)          # total_steps
    aborted     = QtCore.pyqtSignal(int)          # completed_before_abort

    def __init__(self, session_dir: str, parent=None):
        super().__init__(parent)
        self._session_dir = session_dir
        self._abort = threading.Event()
        self._settle_timeout = 60.0        # per motor caput -c timeout

    def request_abort(self) -> None:
        self._abort.set()

    def _load_moves(self) -> list[dict]:
        jsonl = os.path.join(self._session_dir, "actions.jsonl")
        moves = []
        try:
            with open(jsonl) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") == "motor_move":
                        moves.append(row)
        except OSError as e:
            LOGGER.warning("replay: cannot read jsonl: %s", e)
        return moves

    def run(self) -> None:  # QThread.run
        import subprocess
        moves = self._load_moves()
        total = len(moves)
        if total == 0:
            self.progress.emit(0, 0, "no motor_move events in this session")
            self.finished_ok.emit(0)
            return

        for i, move in enumerate(moves, start=1):
            if self._abort.is_set():
                self.aborted.emit(i - 1)
                return

            motors = move.get("motors", [])
            desc = "  ".join(
                f"{m.get('label','?')} → {m.get('to','?')}"
                for m in motors)
            self.progress.emit(i, total, f"step {i}/{total}: {desc}")

            # Drive each motor's setpoint (blocking on DMOV via caput -c).
            # If multiple motors were in the recorded burst, launch them
            # concurrently so they move at the same time as recorded.
            failures = []
            procs = []
            for m in motors:
                pv, target = m.get("pv"), m.get("to")
                if not pv or target is None:
                    failures.append(f"skip (missing pv/to)")
                    continue
                try:
                    p = subprocess.Popen(
                        ["caput", "-c", "-t", pv, str(target)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    procs.append((m, p))
                except FileNotFoundError:
                    failures.append("`caput` not on PATH")
                    break
            for m, p in procs:
                try:
                    p.wait(timeout=self._settle_timeout)
                    if p.returncode != 0:
                        err = p.stderr.read().decode(errors="ignore").strip()
                        failures.append(
                            f"{m.get('label','?')} rc={p.returncode} {err}")
                except subprocess.TimeoutExpired:
                    p.kill()
                    failures.append(f"{m.get('label','?')} timeout")

            if failures:
                self.step_err.emit(i, move, " | ".join(failures))
                # Best-effort: keep going. User can Abort.
            else:
                self.step_ok.emit(i, move)

        self.finished_ok.emit(total)


class ReplayDialog(QtWidgets.QDialog):
    """Progress + abort UI for a running ReplayWorker."""

    def __init__(self, session_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Replay — {os.path.basename(session_dir)}")
        self.resize(720, 420)

        v = QtWidgets.QVBoxLayout(self)
        self._label = QtWidgets.QLabel("starting…")
        self._label.setWordWrap(True)
        v.addWidget(self._label)

        self._bar = QtWidgets.QProgressBar()
        v.addWidget(self._bar)

        self._log = QtWidgets.QListWidget()
        self._log.setStyleSheet("QListWidget { font-family: monospace; }")
        v.addWidget(self._log, 1)

        row = QtWidgets.QHBoxLayout()
        self._abort_btn = QtWidgets.QPushButton("■ Abort")
        self._abort_btn.clicked.connect(self._on_abort)
        self._close_btn = QtWidgets.QPushButton("Close")
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self.accept)
        row.addWidget(self._abort_btn)
        row.addStretch(1)
        row.addWidget(self._close_btn)
        v.addLayout(row)

        self._worker = ReplayWorker(session_dir, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.step_ok.connect(self._on_ok)
        self._worker.step_err.connect(self._on_err)
        self._worker.finished_ok.connect(self._on_done_ok)
        self._worker.aborted.connect(self._on_done_aborted)
        self._worker.start()

    def _on_progress(self, i: int, total: int, msg: str) -> None:
        self._bar.setMaximum(max(1, total))
        self._bar.setValue(i)
        self._label.setText(msg)

    def _on_ok(self, i: int, event: dict) -> None:
        parts = [
            f"{m.get('label','?')} → {m.get('to','?')} (Δ{m.get('delta',0):+.4g})"
            for m in event.get("motors", [])
        ]
        self._log.addItem(f"OK  step {i}: " + "  ".join(parts))

    def _on_err(self, i: int, event: dict, err: str) -> None:
        self._log.addItem(f"ERR step {i}: {err}")

    def _on_done_ok(self, total: int) -> None:
        self._label.setText(f"done — {total} step(s) replayed")
        self._abort_btn.setEnabled(False)
        self._close_btn.setEnabled(True)

    def _on_done_aborted(self, done: int) -> None:
        self._label.setText(f"aborted after {done} step(s)")
        self._abort_btn.setEnabled(False)
        self._close_btn.setEnabled(True)

    def _on_abort(self) -> None:
        self._worker.request_abort()
        self._label.setText("aborting — waiting for current step to end…")

    def closeEvent(self, ev) -> None:
        if self._worker.isRunning():
            resp = QtWidgets.QMessageBox.question(
                self, "Replay in progress",
                "A replay is still running. Abort and close?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            if resp != QtWidgets.QMessageBox.Yes:
                ev.ignore()
                return
            self._worker.request_abort()
            self._worker.wait(2000)
        super().closeEvent(ev)


# ─────────────────────────────────────────────────────────────── browser

class SessionBrowserDialog(QtWidgets.QDialog):
    """Read-only browser for past task recordings. Left panel = task
    list; middle = sessions for that element (newest first); right = the
    README + a compact action list for the selected session."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Browse Task Recordings")
        self.resize(920, 560)
        self._build_ui()
        self._reload_elements()

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self)
        root.addWidget(split, 1)

        # Elements
        el_box = QtWidgets.QWidget()
        el_v = QtWidgets.QVBoxLayout(el_box)
        el_v.setContentsMargins(0, 0, 0, 0)
        el_v.addWidget(QtWidgets.QLabel("Tasks"))
        self._el_list = QtWidgets.QListWidget()
        self._el_list.currentItemChanged.connect(self._on_element_selected)
        el_v.addWidget(self._el_list, 1)
        split.addWidget(el_box)

        # Sessions
        se_box = QtWidgets.QWidget()
        se_v = QtWidgets.QVBoxLayout(se_box)
        se_v.setContentsMargins(0, 0, 0, 0)
        se_v.addWidget(QtWidgets.QLabel("Sessions"))
        self._se_list = QtWidgets.QListWidget()
        self._se_list.currentItemChanged.connect(self._on_session_selected)
        se_v.addWidget(self._se_list, 1)
        split.addWidget(se_box)

        # Preview
        pv_box = QtWidgets.QWidget()
        pv_v = QtWidgets.QVBoxLayout(pv_box)
        pv_v.setContentsMargins(0, 0, 0, 0)
        pv_v.addWidget(QtWidgets.QLabel("Session detail"))
        self._preview = QtWidgets.QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet("QTextEdit { font-family: monospace; }")
        pv_v.addWidget(self._preview, 1)
        split.addWidget(pv_box)

        split.setSizes([200, 240, 480])

        # Bottom row
        btn_row = QtWidgets.QHBoxLayout()
        self._open_dir_btn = QtWidgets.QPushButton("Open folder in file manager")
        self._open_dir_btn.clicked.connect(self._open_current_dir)
        self._open_dir_btn.setEnabled(False)
        self._run_btn = QtWidgets.QPushButton("▶ Run this task")
        self._run_btn.setToolTip(
            "Replay the recorded motor moves. Uses `caput -c` per "
            "motor (blocks on DMOV). You'll be asked to confirm.")
        self._run_btn.clicked.connect(self._run_current)
        self._run_btn.setEnabled(False)
        self._publish_btn = QtWidgets.QPushButton("★ Publish as tool")
        self._publish_btn.setToolTip(
            "Once training is done, publish this session as a named tool "
            "for one-click replay from the Tools dialog. Republishing "
            "under the same name overwrites (e.g. after a PV change).")
        self._publish_btn.clicked.connect(self._publish_current)
        self._publish_btn.setEnabled(False)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._open_dir_btn)
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._publish_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def _reload_elements(self) -> None:
        self._el_list.clear()
        self._se_list.clear()
        self._preview.clear()
        self._open_dir_btn.setEnabled(False)
        if not os.path.isdir(TASK_RECORDINGS_ROOT):
            self._preview.setPlainText(
                f"No recordings yet.\n\nFolder does not exist:\n  {TASK_RECORDINGS_ROOT}\n\n"
                "Record a session with the ● Start recording button.")
            return
        try:
            slugs = sorted(d for d in os.listdir(TASK_RECORDINGS_ROOT)
                           if os.path.isdir(os.path.join(TASK_RECORDINGS_ROOT, d)))
        except OSError:
            slugs = []
        if not slugs:
            self._preview.setPlainText("No recordings yet under\n  " + TASK_RECORDINGS_ROOT)
            return
        for slug in slugs:
            display = self._element_display_name(slug)
            item = QtWidgets.QListWidgetItem(f"{display}  ({self._session_count(slug)})")
            item.setData(QtCore.Qt.UserRole, slug)
            self._el_list.addItem(item)
        self._el_list.setCurrentRow(0)

    def _element_display_name(self, slug: str) -> str:
        """Prefer the display name recorded inside the latest session
        start event; fall back to the slug."""
        elem_dir = os.path.join(TASK_RECORDINGS_ROOT, slug)
        try:
            sessions = sorted(d for d in os.listdir(elem_dir)
                              if os.path.isdir(os.path.join(elem_dir, d)))
        except OSError:
            return slug
        if not sessions:
            return slug
        jsonl = os.path.join(elem_dir, sessions[-1], "actions.jsonl")
        if not os.path.isfile(jsonl):
            return slug
        try:
            with open(jsonl) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") == "session_start":
                        return str(row.get("element") or slug)
                    break
        except OSError:
            pass
        return slug

    def _session_count(self, slug: str) -> int:
        elem_dir = os.path.join(TASK_RECORDINGS_ROOT, slug)
        try:
            return sum(1 for d in os.listdir(elem_dir)
                       if os.path.isdir(os.path.join(elem_dir, d)))
        except OSError:
            return 0

    def _on_element_selected(self, current, _prev) -> None:
        self._se_list.clear()
        self._preview.clear()
        self._open_dir_btn.setEnabled(False)
        if current is None:
            return
        slug = current.data(QtCore.Qt.UserRole)
        elem_dir = os.path.join(TASK_RECORDINGS_ROOT, slug)
        try:
            sessions = sorted((d for d in os.listdir(elem_dir)
                               if os.path.isdir(os.path.join(elem_dir, d))),
                              reverse=True)
        except OSError:
            sessions = []
        for sess_id in sessions:
            summary = self._session_summary(slug, sess_id)
            item = QtWidgets.QListWidgetItem(summary)
            item.setData(QtCore.Qt.UserRole, (slug, sess_id))
            self._se_list.addItem(item)
        if sessions:
            self._se_list.setCurrentRow(0)

    def _session_summary(self, slug: str, sess_id: str) -> str:
        sess_dir = os.path.join(TASK_RECORDINGS_ROOT, slug, sess_id)
        jsonl = os.path.join(sess_dir, "actions.jsonl")
        moves = 0
        note = ""
        try:
            with open(jsonl) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") == "motor_move":
                        moves += 1
                    elif row.get("type") == "session_start" and not note:
                        note = str(row.get("opening_note") or "")
        except OSError:
            pass
        base = f"{sess_id}   ({moves} moves)"
        if note:
            base += "  — " + (note if len(note) <= 40 else note[:37] + "…")
        return base

    def _on_session_selected(self, current, _prev) -> None:
        self._preview.clear()
        self._open_dir_btn.setEnabled(False)
        self._run_btn.setEnabled(False)
        self._publish_btn.setEnabled(False)
        if current is None:
            return
        slug, sess_id = current.data(QtCore.Qt.UserRole)
        sess_dir = os.path.join(TASK_RECORDINGS_ROOT, slug, sess_id)
        self._open_dir_btn.setEnabled(True)
        self._run_btn.setEnabled(True)
        self._publish_btn.setEnabled(True)
        self._current_dir = sess_dir

        parts = [f"=== {sess_dir} ==="]
        readme = os.path.join(sess_dir, "README.md")
        if os.path.isfile(readme):
            try:
                with open(readme) as f:
                    parts.append(f.read().rstrip())
            except OSError as e:
                parts.append(f"(failed to read README.md: {e})")
        parts.append("")
        parts.append("--- ACTIONS ---")

        jsonl = os.path.join(sess_dir, "actions.jsonl")
        if not os.path.isfile(jsonl):
            parts.append("(no actions.jsonl)")
        else:
            try:
                with open(jsonl) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        parts.append(self._format_action(row))
            except OSError as e:
                parts.append(f"(failed to read actions.jsonl: {e})")
        self._preview.setPlainText("\n".join(parts))
        # Scroll to top
        cur = self._preview.textCursor()
        cur.movePosition(QtGui.QTextCursor.Start)
        self._preview.setTextCursor(cur)

    @staticmethod
    def _format_action(row: dict) -> str:
        etype = row.get("type", "?")
        ts = datetime.fromtimestamp(row.get("ts", 0)).strftime("%H:%M:%S")
        if etype == "session_start":
            frame = row.get("start_frame") or "-"
            return f"{ts}  START  element={row.get('element','')}  frame={frame}  note={row.get('opening_note','')}"
        if etype == "motor_move":
            motors = row.get("motors", [])
            parts = [f"{m['label']} {m['from']:.4g}→{m['to']:.4g} (Δ{m['delta']:+.4g})"
                     for m in motors]
            return (f"{ts}  MOVE   " + "  ".join(parts)
                    + f"   [frame={row.get('frame','-')}]")
        if etype == "snapshot":
            return f"{ts}  SNAP   tag={row.get('tag','')}  frame={row.get('frame','-')}"
        if etype == "note":
            return f"{ts}  NOTE   {row.get('text','')}"
        if etype == "session_end":
            return (f"{ts}  END    moves={row.get('moves','?')}  "
                    f"elapsed={row.get('elapsed_s','?')}s  "
                    f"note={row.get('closing_note','')}")
        return f"{ts}  {etype.upper()}  {row}"

    def _run_current(self) -> None:
        """Confirm + spawn a ReplayWorker for the selected session."""
        d = getattr(self, "_current_dir", None)
        if not d:
            return
        # Count what will be moved so the confirmation is concrete.
        moves = 0
        pvs = set()
        try:
            with open(os.path.join(d, "actions.jsonl")) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") == "motor_move":
                        moves += 1
                        for m in row.get("motors", []):
                            if m.get("pv"):
                                pvs.add(m["pv"])
        except OSError:
            pass
        if moves == 0:
            QtWidgets.QMessageBox.information(
                self, "Replay", "This session has no motor_move events.")
            return
        pv_list = "\n  ".join(sorted(pvs)) or "(none)"
        resp = QtWidgets.QMessageBox.warning(
            self, "Confirm replay",
            f"About to REPLAY this recorded task.\n\n"
            f"  {moves} step(s) will be executed with `caput -c`\n"
            f"  Motors involved:\n  {pv_list}\n\n"
            f"Absolute motor positions from the recording will be commanded.\n"
            f"Make sure the beamline is in a state where those positions\n"
            f"are safe. Continue?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        dlg = ReplayDialog(d, self)
        dlg.exec_()

    def _publish_current(self) -> None:
        """Turn the selected session into a named tool. Default name is
        the recorded element display name. Republishing the same name
        overwrites — that's the intended workflow when the underlying
        PVs or hardware change and the user re-trains."""
        d = getattr(self, "_current_dir", None)
        if not d:
            return
        try:
            record = _tool_from_session(d)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(
                self, "Publish failed", str(e))
            return

        default_name = record["element"] or record["element_slug"]
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Publish as tool",
            "Name for this alignment tool "
            "(re-use an existing name to overwrite):",
            text=default_name)
        if not ok or not name.strip():
            return
        name = name.strip()

        tools = _load_tools()
        if name in tools:
            existing = tools[name]
            resp = QtWidgets.QMessageBox.question(
                self, "Overwrite tool",
                f"Tool “{name}” already exists (session "
                f"{existing.get('session_id','?')}, "
                f"{existing.get('n_moves','?')} moves).\n\n"
                f"Overwrite with the current session "
                f"({record['session_id']}, {record['n_moves']} moves)?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if resp != QtWidgets.QMessageBox.Yes:
                return

        # Optional description — starts blank so it's fast to accept.
        desc, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "Description (optional)",
            "One-line description shown in the Tools list:")
        if ok:
            record["description"] = desc.strip()

        tools[name] = record
        _save_tools(tools)
        QtWidgets.QMessageBox.information(
            self, "Published",
            f"Tool “{name}” saved. Run it any time from the "
            f"“🛠 Tools” button in the main Task Recorder dialog.")

    def _open_current_dir(self) -> None:
        d = getattr(self, "_current_dir", None)
        if not d:
            return
        import subprocess
        try:
            subprocess.Popen(["xdg-open", d])
        except FileNotFoundError:
            QtWidgets.QMessageBox.information(
                self, "Task Recorder",
                f"Folder path (no xdg-open available):\n\n{d}")


# ─────────────────────────────────────────────────────── published tools

class PublishedToolsDialog(QtWidgets.QDialog):
    """One-click replay UI for blessed alignment tools. Each row is a
    published tool: name, source element + session id, motor count, and
    a ▶ Run button that pops the same ReplayDialog used in the browser.
    ✕ deletes a tool from the store; ℹ shows its detail (motors, source
    session, description). Tool store is `~/.pystream/task_tools.json`."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Published Task Tools")
        self.resize(720, 420)
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        v = QtWidgets.QVBoxLayout(self)

        v.addWidget(QtWidgets.QLabel(
            "One-click replay of blessed alignment procedures. Publish a "
            "new tool from the 📂 Load dialog's ★ Publish button."))

        self._table = QtWidgets.QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Tool name", "Source task", "Session", "Moves", ""])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            4, QtWidgets.QHeaderView.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        self._table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers)
        v.addWidget(self._table, 1)

        self._empty_label = QtWidgets.QLabel(
            "<i>No tools published yet. "
            "Record a session, load it via 📂, and press ★ Publish as tool.</i>")
        self._empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self._empty_label.setStyleSheet("color: #888;")
        v.addWidget(self._empty_label)

        btn_row = QtWidgets.QHBoxLayout()
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)
        v.addLayout(btn_row)

    def _reload(self) -> None:
        tools = _load_tools()
        self._table.setRowCount(0)
        self._empty_label.setVisible(not tools)
        self._table.setVisible(bool(tools))
        for name in sorted(tools.keys(), key=str.lower):
            record = tools[name]
            row = self._table.rowCount()
            self._table.insertRow(row)

            name_item = QtWidgets.QTableWidgetItem(name)
            name_item.setToolTip(record.get("description") or "(no description)")
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, QtWidgets.QTableWidgetItem(
                str(record.get("element", ""))))
            self._table.setItem(row, 2, QtWidgets.QTableWidgetItem(
                str(record.get("session_id", ""))))
            self._table.setItem(row, 3, QtWidgets.QTableWidgetItem(
                str(record.get("n_moves", "?"))))

            # Action buttons cell
            cell = QtWidgets.QWidget()
            cl = QtWidgets.QHBoxLayout(cell)
            cl.setContentsMargins(2, 2, 2, 2)
            cl.setSpacing(4)
            run_btn = QtWidgets.QPushButton("▶ Run")
            run_btn.setToolTip("Replay this tool now with `caput -c`")
            run_btn.clicked.connect(lambda _=False, n=name: self._run_tool(n))
            info_btn = QtWidgets.QPushButton("ℹ")
            info_btn.setToolTip("Show source, motors, description")
            info_btn.setMaximumWidth(28)
            info_btn.clicked.connect(lambda _=False, n=name: self._info_tool(n))
            del_btn = QtWidgets.QPushButton("✕")
            del_btn.setToolTip("Delete this tool (source recording is kept)")
            del_btn.setMaximumWidth(28)
            del_btn.clicked.connect(lambda _=False, n=name: self._delete_tool(n))
            cl.addWidget(run_btn)
            cl.addWidget(info_btn)
            cl.addWidget(del_btn)
            self._table.setCellWidget(row, 4, cell)

    def _run_tool(self, name: str) -> None:
        tools = _load_tools()
        record = tools.get(name)
        if not record:
            return
        session_dir = record.get("session_dir")
        if not session_dir or not os.path.isdir(session_dir):
            QtWidgets.QMessageBox.warning(
                self, "Broken tool",
                f"Tool “{name}” points to a session directory that no "
                f"longer exists:\n  {session_dir}\n\n"
                f"Delete the tool (✕) or re-publish it from a live session.")
            return

        motors = record.get("motors", [])
        pv_list = "\n  ".join(sorted({m.get("pv","?") for m in motors})) or "(none)"
        resp = QtWidgets.QMessageBox.warning(
            self, f"Run “{name}”",
            f"About to REPLAY tool “{name}”.\n\n"
            f"  Source session: {record.get('element','')} / "
            f"{record.get('session_id','')}\n"
            f"  {record.get('n_moves','?')} recorded step(s)\n"
            f"  Motors involved:\n  {pv_list}\n\n"
            f"Absolute motor positions from the recording will be "
            f"commanded via `caput -c`.\n"
            f"Make sure the beamline is in a state where those positions "
            f"are safe. Continue?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        dlg = ReplayDialog(session_dir, self)
        dlg.exec_()

    def _info_tool(self, name: str) -> None:
        record = _load_tools().get(name)
        if not record:
            return
        motors = record.get("motors", [])
        motor_lines = "\n".join(
            f"  - {m.get('label','?')}  ({m.get('pv','?')})"
            for m in motors) or "  (none)"
        ts = record.get("published_ts")
        published = (datetime.fromtimestamp(ts).isoformat(timespec='seconds')
                     if ts else "unknown")
        desc = record.get("description") or "(none)"
        QtWidgets.QMessageBox.information(
            self, f"Tool “{name}”",
            f"<b>Source task:</b> {record.get('element','')}<br>"
            f"<b>Source session:</b> {record.get('session_id','')}<br>"
            f"<b>Session dir:</b> {record.get('session_dir','')}<br>"
            f"<b>Recorded moves:</b> {record.get('n_moves','?')}<br>"
            f"<b>Published:</b> {published}<br>"
            f"<b>Description:</b> {desc}<br><br>"
            f"<b>Motors:</b><br><pre>{motor_lines}</pre>")

    def _delete_tool(self, name: str) -> None:
        resp = QtWidgets.QMessageBox.question(
            self, "Delete tool",
            f"Delete tool “{name}” from the store?\n\n"
            f"The underlying recorded session on disk is NOT deleted — "
            f"you can re-publish it later.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        tools = _load_tools()
        tools.pop(name, None)
        _save_tools(tools)
        self._reload()


