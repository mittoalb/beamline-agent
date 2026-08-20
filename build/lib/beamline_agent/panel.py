"""Live "who's running" window for pystream.

Renders the shared agent-status registry (`~/.aps_agents/agents.json`)
as a compact list of cards, one per agent, indented under their
parents so parent/child relationships are visible at a glance. Auto-
updates on file changes via QFileSystemWatcher plus a 1 s fallback
poll for filesystems where change notifications aren't reliable
(NFS in particular).

Ships as a standalone window opened from pystream's top toolbar
(`👥 Agents` button). Uses an explicit dark palette so it reads as
a monitor / status console rather than a chat surface, independent
of the host Qt theme. Any process that writes a record into the
registry — pystream itself, a spawned sub-agent, a headless
`tomogui-batch` on another host with NFS-shared home — shows up
here without further wiring.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from .status import (
    APS_AGENTS_DIR, AGENTS_FILE, DEFAULT_LINGER_S,
    load_registry, purge_stale_records,
)

# Records whose owning process is very likely gone: past ttl_s AND past
# this extra grace window on top. Passed to `purge_stale_records`; the
# panel calls it on open and every 60 s so the display self-heals.
STALE_PURGE_GRACE_S = 300      # 5 minutes past ttl → assume owner is dead

# State → (color, label) for the left-border indicator and the small dot.
# Bright vibrant colors so they pop against the black background.
_STATE_COLORS = {
    "running":  ("#4ade80", "running"),   # bright green
    "idle":     ("#6b7280", "idle"),      # cool gray
    "waiting":  ("#60a5fa", "waiting"),   # bright blue
    "starting": ("#c084fc", "starting"),  # bright purple
    "done":     ("#94a3b8", "done"),      # slate
    "error":    ("#f87171", "error"),     # bright red
    "stale":    ("#fbbf24", "stale"),     # amber
}

_INDENT_PX     = 22        # per parent-depth level
_POLL_MS       = 1000
_MAX_ACTIVITY  = 100       # truncate the activity line to keep cards compact

# Dark palette — applied explicitly on every widget so we don't inherit
# whatever the host Qt theme is.
_BG_WINDOW     = "#000000"
_BG_CARD       = "#181818"
_BG_CARD_ALT   = "#101010"
_BORDER        = "#2a2a2a"
_TEXT_MAIN     = "#e5e5e5"
_TEXT_META     = "#8a8a8a"
_TEXT_ACTIVITY = "#c0c0c0"
_TEXT_DIM      = "#666666"

_DIALOG_QSS = f"""
QDialog, QWidget#agents_root {{ background: {_BG_WINDOW}; color: {_TEXT_MAIN}; }}
QScrollArea {{ background: {_BG_WINDOW}; border: none; }}
QScrollArea > QWidget > QWidget {{ background: {_BG_WINDOW}; }}
QLabel {{ color: {_TEXT_MAIN}; }}
QToolButton {{
    background: {_BG_CARD};
    color: {_TEXT_MAIN};
    border: 1px solid {_BORDER};
    border-radius: 3px;
    padding: 2px 6px;
}}
QToolButton:hover {{ background: {_BG_CARD_ALT}; border-color: #444; }}
QScrollBar:vertical {{
    background: {_BG_WINDOW}; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 3px; min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: #3a3a3a; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QProgressBar {{
    background: {_BG_CARD_ALT};
    border: 1px solid {_BORDER};
    border-radius: 2px;
    color: {_TEXT_MAIN};
    text-align: center;
    font-size: 9px;
}}
QProgressBar::chunk {{ background: #4ade80; }}
"""


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 0:
        return "?"
    if seconds < 60:
        return f"{seconds:0.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


class _AgentCard(QtWidgets.QFrame):
    """One row: state dot + name + host + activity + progress + elapsed."""

    def __init__(self, record: dict, depth: int = 0, parent=None):
        super().__init__(parent)
        self._record = record
        self._depth = depth
        color, _ = _STATE_COLORS.get(record.get("state", "?"),
                                     _STATE_COLORS["stale"])
        # Explicit dark card — background, 4-px accent border on the
        # left in the state color, thin subtle border elsewhere.
        self.setStyleSheet(
            f"QFrame {{ background: {_BG_CARD}; "
            f"color: {_TEXT_MAIN}; "
            f"border-left: 4px solid {color}; "
            f"border-top: 1px solid {_BORDER}; "
            f"border-right: 1px solid {_BORDER}; "
            f"border-bottom: 1px solid {_BORDER}; "
            f"border-radius: 3px; }}"
        )
        self._build_ui(color)

    def _build_ui(self, color: str) -> None:
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(6 + self._depth * _INDENT_PX, 4, 8, 4)
        outer.setSpacing(8)

        # State dot — override the parent's border rules so it's a
        # clean colored circle rather than inheriting the card outline.
        dot = QtWidgets.QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(
            f"background: {color}; border: none; border-radius: 5px;")
        outer.addWidget(dot, 0, QtCore.Qt.AlignTop)

        # Text column
        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(1)

        # Line 1: bold name  ·  small host  ·  small kind    elapsed
        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(6)
        name_lbl = QtWidgets.QLabel(
            f"<b>{self._escape(self._record.get('name', '?'))}</b>")
        name_lbl.setStyleSheet(f"color: {_TEXT_MAIN}; border: none;")
        row1.addWidget(name_lbl)
        host = self._record.get("host") or ""
        kind = self._record.get("kind") or ""
        meta_bits = []
        if host:
            meta_bits.append(host)
        if kind:
            meta_bits.append(kind)
        if meta_bits:
            meta_lbl = QtWidgets.QLabel(
                f"<span style='color:{_TEXT_META}; font-size:10px;'>"
                f"· {self._escape(' · '.join(meta_bits))}</span>")
            meta_lbl.setStyleSheet("border: none;")
            row1.addWidget(meta_lbl)
        row1.addStretch(1)
        # elapsed (state-time)
        started = self._record.get("started_ts", time.time())
        elapsed_lbl = QtWidgets.QLabel(
            f"<span style='color:{_TEXT_DIM}; font-size:10px;'>"
            f"{_fmt_elapsed(time.time() - started)}</span>")
        elapsed_lbl.setStyleSheet("border: none;")
        row1.addWidget(elapsed_lbl)
        body.addLayout(row1)

        # Line 2: activity (may be truncated)
        activity = str(self._record.get("activity") or "").strip()
        if len(activity) > _MAX_ACTIVITY:
            activity = activity[:_MAX_ACTIVITY - 1] + "…"
        act_lbl = QtWidgets.QLabel(self._escape(activity))
        act_lbl.setStyleSheet(
            f"color: {_TEXT_ACTIVITY}; font-size: 11px; border: none;")
        act_lbl.setWordWrap(False)
        body.addWidget(act_lbl)

        # Line 3: progress bar (only if attached)
        prog = self._record.get("progress")
        if isinstance(prog, dict) and prog.get("total"):
            done = int(prog.get("done") or 0)
            total = int(prog["total"])
            bar = QtWidgets.QProgressBar()
            bar.setMinimum(0)
            bar.setMaximum(total)
            bar.setValue(min(done, total))
            bar.setFormat(f"{done}/{total}  (%p%)")
            bar.setFixedHeight(10)
            body.addWidget(bar)

        outer.addLayout(body, 1)

        # Full record on hover (audit tail)
        tip_lines = [
            f"id: {self._record.get('id')}",
            f"parent: {self._record.get('parent') or '(root)'}",
            f"state: {self._record.get('state')}",
            f"host: {self._record.get('host')}",
            f"started: {time.strftime('%H:%M:%S', time.localtime(self._record.get('started_ts', 0)))}",
            f"updated: {time.strftime('%H:%M:%S', time.localtime(self._record.get('updated_ts', 0)))}",
        ]
        self.setToolTip("\n".join(tip_lines))

    @staticmethod
    def _escape(s: str) -> str:
        return (s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))


class AgentsPanel(QtWidgets.QWidget):
    """Bottom-panel widget rendering the live agent registry."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Distinct object name so the dialog's QSS can target the panel
        # root even when embedded inside a QDialog.
        self.setObjectName("agents_root")
        self._build_ui()

        # File watcher — watch the DIRECTORY too, because the file
        # doesn't exist at pystream startup (created on first publish).
        self._watcher = QtCore.QFileSystemWatcher(self)
        try:
            os.makedirs(APS_AGENTS_DIR, exist_ok=True)
        except OSError:
            pass
        if os.path.isdir(APS_AGENTS_DIR):
            self._watcher.addPath(APS_AGENTS_DIR)
        if os.path.isfile(AGENTS_FILE):
            self._watcher.addPath(AGENTS_FILE)
        self._watcher.directoryChanged.connect(self._on_watch)
        self._watcher.fileChanged.connect(self._on_watch)

        # Fallback poll — NFS often doesn't fire inotify events,
        # and elapsed-time labels need periodic refresh anyway.
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._reload)
        self._timer.start(_POLL_MS)

        # Periodic janitor: purge records whose owning process is
        # very likely gone (past ttl + grace). Runs every 60 s and
        # once at startup so the panel self-heals across restarts.
        self._purge_timer = QtCore.QTimer(self)
        self._purge_timer.timeout.connect(self._purge_now)
        self._purge_timer.start(60_000)
        QtCore.QTimer.singleShot(0, self._purge_now)

        self._reload()

    def _purge_now(self) -> None:
        """Sweep the registry file for records whose owner has been
        silent past ttl + grace. Rewrites the file atomically; no-op
        when nothing needs dropping. Cheap enough to call on demand."""
        try:
            dropped = purge_stale_records(stale_grace_s=STALE_PURGE_GRACE_S)
        except Exception:
            dropped = 0
        if dropped:
            self._reload()

    def _build_ui(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        title = QtWidgets.QLabel("<b>Agents</b>")
        title.setStyleSheet(f"color: {_TEXT_MAIN}; font-size: 12px;")
        header.addWidget(title)
        self._summary = QtWidgets.QLabel("")
        self._summary.setStyleSheet(f"color: {_TEXT_META}; font-size: 11px;")
        header.addWidget(self._summary)
        header.addStretch(1)
        purge_btn = QtWidgets.QToolButton()
        purge_btn.setText("🧹")
        purge_btn.setToolTip(
            "Purge stale records — drops entries from crashed/killed "
            "agents that never got a chance to mark themselves done. "
            "Auto-runs every minute; press to force it now.")
        purge_btn.clicked.connect(self._purge_now)
        header.addWidget(purge_btn)
        refresh_btn = QtWidgets.QToolButton()
        refresh_btn.setText("⟳")
        refresh_btn.setToolTip("Force refresh")
        refresh_btn.clicked.connect(self._reload)
        header.addWidget(refresh_btn)
        outer.addLayout(header)

        self._empty_label = QtWidgets.QLabel(
            "<i>No agents publishing yet. When pystream's AI panel or a "
            "spawned sub-agent starts running, it will appear here.</i>")
        self._empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {_TEXT_META};")
        self._empty_label.setWordWrap(True)
        outer.addWidget(self._empty_label)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setStyleSheet(f"background: {_BG_WINDOW}; border: none;")
        self._container = QtWidgets.QWidget()
        self._container.setStyleSheet(f"background: {_BG_WINDOW};")
        self._card_layout = QtWidgets.QVBoxLayout(self._container)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(3)
        self._card_layout.addStretch(1)
        scroll.setWidget(self._container)
        outer.addWidget(scroll, 1)

        self.setMinimumHeight(120)
        # Force the panel's own background too, in case it's rendered
        # outside the AgentsDialog (e.g. dropped into a bottom dock).
        self.setStyleSheet(_DIALOG_QSS)

    # ── data → view ────────────────────────────────────────────────

    def _reload(self) -> None:
        raw = load_registry()
        agents = self._filter_and_annotate(raw)
        self._render(agents)
        self._refresh_watch()

    def _filter_and_annotate(self, raw: Dict[str, dict]) -> List[dict]:
        """Purge stale done/error records past their linger; mark
        running-but-silent records as "stale" so the panel dims them
        without dropping them."""
        now = time.time()
        keep: List[dict] = []
        for rec in raw.values():
            if not isinstance(rec, dict):
                continue
            state = rec.get("state") or "?"
            updated = float(rec.get("updated_ts") or 0)
            age = now - updated
            if state in ("done", "error"):
                linger = float(rec.get("linger_s") or DEFAULT_LINGER_S)
                if age > linger:
                    continue        # purge from view
            else:
                ttl = float(rec.get("ttl_s") or 30)
                if age > ttl:
                    rec = dict(rec, state="stale")
            keep.append(rec)
        return keep

    def _render(self, agents: List[dict]) -> None:
        # Clear existing cards (keep the trailing stretch)
        while self._card_layout.count() > 1:
            item = self._card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not agents:
            self._empty_label.show()
            self._summary.setText("")
            return
        self._empty_label.hide()

        # Sort into a parent-child tree, then flatten depth-first.
        # Roots = agents whose parent is None OR whose parent isn't in
        # the current registry (an orphan is treated as a root so it
        # still shows up when the parent has already purged).
        by_id = {a.get("id"): a for a in agents if a.get("id")}
        children: Dict[Optional[str], List[dict]] = {}
        for a in agents:
            parent_id = a.get("parent")
            key = parent_id if parent_id in by_id else None
            children.setdefault(key, []).append(a)

        # Sort each level by started_ts (older = further up)
        for group in children.values():
            group.sort(key=lambda r: r.get("started_ts") or 0)

        flat: List[tuple] = []
        def _walk(parent_key: Optional[str], depth: int) -> None:
            for a in children.get(parent_key, []):
                flat.append((a, depth))
                _walk(a.get("id"), depth + 1)
        _walk(None, 0)

        insert_at = 0
        for rec, depth in flat:
            card = _AgentCard(rec, depth=depth, parent=self._container)
            self._card_layout.insertWidget(insert_at, card)
            insert_at += 1

        # Summary line — counts by state
        states: Dict[str, int] = {}
        for a in agents:
            states[a.get("state") or "?"] = states.get(a.get("state") or "?", 0) + 1
        parts = [f"{n} {s}" for s, n in
                 sorted(states.items(), key=lambda p: -p[1])]
        self._summary.setText(" · ".join(parts))

    def _refresh_watch(self) -> None:
        """Re-add the file to the watcher if it appeared after startup."""
        if (os.path.isfile(AGENTS_FILE)
                and AGENTS_FILE not in self._watcher.files()):
            self._watcher.addPath(AGENTS_FILE)

    def _on_watch(self, _path: str) -> None:
        # File may have been atomically replaced (rename removes+creates),
        # which drops it from the watcher. _reload → _refresh_watch fixes.
        self._reload()


def build_agents_panel(parent_window) -> QtWidgets.QWidget:
    """Factory used when the panel is embedded in another container
    (e.g. a bottom dock). Kept for external callers; pystream itself
    now uses `AgentsDialog` for its top-toolbar entry point."""
    w = AgentsPanel(parent_window)
    w.setMinimumHeight(90)
    w.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                    QtWidgets.QSizePolicy.Expanding)
    return w


# ─────────────────────────────────────────────────────── dialog wrapper

class AgentsDialog(QtWidgets.QDialog):
    """Standalone window wrapping the AgentsPanel with an explicit dark
    palette. Non-modal — the user can leave it open on a second monitor
    while doing other things in pystream. Opened from pystream's top
    toolbar via the `👥 Agents` button; the main window keeps a single
    reference so re-clicking the button just re-focuses instead of
    stacking dialogs.

    Kept as its own class rather than styling AgentsPanel inline so a
    future graphical (`QGraphicsView`-based) view can slot into the
    same dialog by swapping the child widget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agents")
        # Non-modal, independent window (not a modal dialog stacked
        # on the main window). Qt.Window ensures a proper taskbar
        # entry + minimize/maximize buttons.
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowMinimizeButtonHint
            | QtCore.Qt.WindowMaximizeButtonHint
            | QtCore.Qt.WindowCloseButtonHint)
        self.setModal(False)
        self.resize(760, 520)
        self.setStyleSheet(_DIALOG_QSS)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._panel = AgentsPanel(self)
        layout.addWidget(self._panel)
