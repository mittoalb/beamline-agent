"""Live console window for pystream's AI agent.

Shows every tool call the model makes, every tool result it gets back,
and every user/assistant message — with timestamps, in a dark monospace
terminal-style view. Non-modal standalone window opened from the top
toolbar via `📜 Console` (alongside `👥 Agents`).

Why this exists: the chat transcript truncates long stdout, hides tool
arguments unless "show tool calls" is checked, and mixes internals with
the conversation. When you're debugging why the agent's spinning
(bash timing out? model choosing the wrong tool? redundant verification
loop?), you need the raw wire trace. This window is that trace.

Wiring: every `AgentChatWidget` in the main window (dock + popup)
emits `tool_event`, `user_sent`, `assistant_replied`, `error_raised`.
The console iterates `parent.findChildren(AgentChatWidget)` on open
and connects — new widgets appearing later are picked up on the next
open. Lightweight enough to leave visible on a second monitor.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import List

from PyQt5 import QtCore, QtGui, QtWidgets


# ── palette (matches Agents dialog for visual consistency) ────────────
_BG_WINDOW    = "#000000"
_BG_INPUT     = "#0e0e0e"
_BORDER       = "#2a2a2a"
_TEXT_MAIN    = "#e5e5e5"
_TEXT_DIM     = "#666666"

# Line-type colors — chosen for high contrast on black.
_COL_TIME     = "#666666"
_COL_USER     = "#c084fc"    # bright purple
_COL_ASSIST   = "#e5e5e5"    # main
_COL_TOOL_CALL   = "#60a5fa" # bright blue
_COL_TOOL_OK     = "#4ade80" # bright green
_COL_TOOL_ERR    = "#f87171" # bright red
_COL_TOOL_META   = "#fbbf24" # amber (timeout, size warnings)
_COL_ERROR    = "#f87171"

_MAX_EVENTS   = 5000   # ring-buffer cap — older events roll off
_MAX_LINE_CHARS = 4000  # per-event cap so a huge tool result can't blow up rendering

_CONSOLE_QSS = f"""
QDialog {{ background: {_BG_WINDOW}; color: {_TEXT_MAIN}; }}
QPlainTextEdit {{
    background: {_BG_INPUT};
    color: {_TEXT_MAIN};
    border: 1px solid {_BORDER};
    selection-background-color: #2a4d6a;
    font-family: "Menlo", "DejaVu Sans Mono", "Consolas", monospace;
    font-size: 12px;
}}
QLabel {{ color: {_TEXT_MAIN}; }}
QToolButton, QPushButton {{
    background: {_BG_INPUT};
    color: {_TEXT_MAIN};
    border: 1px solid {_BORDER};
    border-radius: 3px;
    padding: 3px 8px;
}}
QToolButton:hover, QPushButton:hover {{ background: #1a1a1a; border-color: #444; }}
QToolButton:checked {{ background: #1e3a5f; border-color: #60a5fa; }}
QCheckBox {{ color: {_TEXT_MAIN}; font-size: 11px; }}
QScrollBar:vertical {{
    background: {_BG_WINDOW}; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 3px; min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: #3a3a3a; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


def _fmt_args(args: dict) -> str:
    """One-line JSON of args, sensibly truncated."""
    try:
        s = json.dumps(args, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        s = repr(args)
    if len(s) > _MAX_LINE_CHARS:
        s = s[:_MAX_LINE_CHARS - 1] + "…"
    return s


def _summarize_args_short(name: str, args: dict) -> str:
    """One-line, human-friendly summary of a tool call's args. Used in
    the in-flight strip where a full JSON dump would wrap and clutter.
    For `bash`, shows the head of the command + timeout; for others,
    the first two args as key=value."""
    if not isinstance(args, dict) or not args:
        return ""
    if name == "bash":
        cmd = str(args.get("command", ""))
        head = cmd if len(cmd) <= 80 else cmd[:79] + "…"
        t = args.get("timeout")
        return f"({head})" + (f"  timeout={t}s" if t else "")
    parts = []
    for k, v in list(args.items())[:2]:
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:39] + "…"
        parts.append(f"{k}={sv}")
    return "(" + ", ".join(parts) + ")"


def _fmt_result(result) -> str:
    """Result blob → readable multi-line summary. Structured dicts get
    formatted; strings pass through; everything else `repr`'d. Big
    stdout blocks stay full-width so the user can actually READ what
    tomogui-cli or ssh returned."""
    if isinstance(result, dict):
        parts = []
        # Prefer common keys in a readable order
        for k in ("returncode", "success", "error", "elapsed_s"):
            if k in result:
                parts.append(f"{k}={result[k]!r}")
        head = " · ".join(parts)
        body_lines = []
        for k in ("stdout", "stderr", "text", "content", "output"):
            v = result.get(k)
            if v is None or v == "":
                continue
            block = str(v)
            if len(block) > _MAX_LINE_CHARS:
                block = block[:_MAX_LINE_CHARS] + "…"
            body_lines.append(f"── {k} ──\n{block}")
        other = {k: v for k, v in result.items()
                 if k not in ("returncode", "success", "error", "elapsed_s",
                              "stdout", "stderr", "text", "content", "output",
                              "command")}
        if other:
            try:
                body_lines.append("── other ──\n"
                                  + json.dumps(other, indent=2, default=str))
            except (TypeError, ValueError):
                body_lines.append("── other ──\n" + repr(other))
        body = "\n".join(body_lines)
        text = head + ("\n" + body if body else "")
    else:
        text = repr(result)
    if len(text) > _MAX_LINE_CHARS * 2:
        text = text[:_MAX_LINE_CHARS * 2] + "\n… (truncated)"
    return text


class AgentConsoleDialog(QtWidgets.QDialog):
    """Live wire-trace viewer for pystream's AI agent."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agent Console")
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowMinimizeButtonHint
            | QtCore.Qt.WindowMaximizeButtonHint
            | QtCore.Qt.WindowCloseButtonHint
        )
        self.setModal(False)
        self.resize(1000, 620)
        self.setStyleSheet(_CONSOLE_QSS)

        self._events: List[dict] = []        # ring buffer
        self._auto_scroll = True
        self._filter_tools_only = False
        self._connected_widgets: List[QtCore.QObject] = []
        # tool_call_id → started_ts. Populated on tool_call, cleared
        # on tool_result. Used to render the live "in flight" strip
        # showing which tools have been running and for how long.
        self._in_flight: dict[str, dict] = {}
        self._call_seq = 0

        self._build_ui()
        # 1-second timer to refresh elapsed-time text on the in-flight
        # strip. Lightweight — only touches labels, no re-render.
        self._tick_timer = QtCore.QTimer(self)
        self._tick_timer.timeout.connect(self._refresh_in_flight_strip)
        self._tick_timer.start(1000)
        self._wire_to_agents()

    def _build_ui(self) -> None:
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        # Header row: title + counts + controls
        hdr = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("<b>Agent Console</b>")
        hdr.addWidget(title)
        self._counts = QtWidgets.QLabel("")
        self._counts.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px;")
        hdr.addWidget(self._counts)
        hdr.addStretch(1)

        self._filter_tools = QtWidgets.QCheckBox("Tool calls only")
        self._filter_tools.setToolTip(
            "Hide user/assistant text — show only tool calls + results.")
        self._filter_tools.toggled.connect(self._on_filter_toggled)
        hdr.addWidget(self._filter_tools)

        self._pause_scroll = QtWidgets.QCheckBox("Pause auto-scroll")
        self._pause_scroll.setToolTip(
            "Stop the view from jumping to the bottom on each new event.")
        self._pause_scroll.toggled.connect(self._on_pause_toggled)
        hdr.addWidget(self._pause_scroll)

        rewire_btn = QtWidgets.QToolButton()
        rewire_btn.setText("⟳")
        rewire_btn.setToolTip("Re-scan for AI widgets (if you opened the popup after this window)")
        rewire_btn.clicked.connect(self._wire_to_agents)
        hdr.addWidget(rewire_btn)

        clear_btn = QtWidgets.QToolButton()
        clear_btn.setText("clear")
        clear_btn.setToolTip("Clear the console view (does not affect chat)")
        clear_btn.clicked.connect(self._clear)
        hdr.addWidget(clear_btn)

        save_btn = QtWidgets.QToolButton()
        save_btn.setText("save…")
        save_btn.setToolTip("Save the current console log to a file")
        save_btn.clicked.connect(self._save_to_file)
        hdr.addWidget(save_btn)

        v.addLayout(hdr)

        # Info line — which widgets are hooked up
        self._info_lbl = QtWidgets.QLabel("")
        self._info_lbl.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 10px;")
        v.addWidget(self._info_lbl)

        # Live "in-flight" strip — one line per currently-running tool
        # call, updated every second. Answers "is the agent still
        # actually doing something, or is it stuck?" without needing
        # to read logs on the remote host.
        self._in_flight_lbl = QtWidgets.QLabel("")
        self._in_flight_lbl.setStyleSheet(
            f"color: {_COL_TOOL_CALL}; font-family: monospace; "
            "font-size: 11px; padding: 2px 4px;")
        self._in_flight_lbl.setWordWrap(True)
        self._in_flight_lbl.setVisible(False)  # hidden when nothing running
        v.addWidget(self._in_flight_lbl)

        # The scrollable log itself
        self._view = QtWidgets.QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self._view.setMaximumBlockCount(_MAX_EVENTS * 4)  # ~4 lines per event avg
        v.addWidget(self._view, 1)

    # ── connecting to AgentChatWidget instances ────────────────────

    def _wire_to_agents(self) -> None:
        """Find every AgentChatWidget in the parent window and connect
        to its signals. Idempotent — repeated connect() calls on Qt
        signals with the same slot are a no-op."""
        try:
            from .chat import AgentChatWidget
        except Exception:
            self._info_lbl.setText("(pystream.agent not importable — no wiring)")
            return

        parent = self.parent()
        # Walk up to find the top-level main window
        top = parent
        while top is not None and top.parent() is not None:
            top = top.parent()
        candidates: List[AgentChatWidget] = []
        if top is not None:
            candidates = top.findChildren(AgentChatWidget)
        # Also include our direct parent if it IS an AgentChatWidget
        if isinstance(parent, AgentChatWidget) and parent not in candidates:
            candidates.append(parent)

        for w in candidates:
            if w in self._connected_widgets:
                continue
            try:
                w.tool_event.connect(self._on_tool_event)
                w.user_sent.connect(self._on_user_sent)
                w.assistant_replied.connect(self._on_assistant_replied)
                w.error_raised.connect(self._on_error)
                self._connected_widgets.append(w)
            except Exception:
                continue

        n = len(self._connected_widgets)
        if n == 0:
            self._info_lbl.setText(
                "(no AI widgets found yet — reopen the console after the "
                "AI panel is up)")
        else:
            names = ", ".join(sorted({w.__class__.__name__ for w in self._connected_widgets}))
            self._info_lbl.setText(f"listening on {n} widget(s) — {names}")

    # ── event handlers (Qt slots) ──────────────────────────────────

    @QtCore.pyqtSlot(str)
    def _on_user_sent(self, text: str) -> None:
        self._push(kind="user", text=text)

    @QtCore.pyqtSlot(str, dict)
    def _on_assistant_replied(self, text: str, usage: dict) -> None:
        meta = ""
        if usage:
            meta = f" [in={usage.get('input',0)} out={usage.get('output',0)}"
            if usage.get("cache_read"):
                meta += f" cache_read={usage['cache_read']}"
            meta += "]"
        self._push(kind="assistant", text=text + meta)

    @QtCore.pyqtSlot(str, dict, object)
    def _on_tool_event(self, name: str, args: dict, result) -> None:
        if result is None:
            self._call_seq += 1
            call_id = f"{name}-{self._call_seq}"
            self._in_flight[call_id] = {
                "name": name,
                "args_summary": _summarize_args_short(name, args),
                "started_ts": time.time(),
            }
            self._push(kind="tool_call", tool=name, args=args, _call_id=call_id)
        else:
            # Match the most recent in-flight entry for this tool name
            match = None
            for cid, rec in list(self._in_flight.items()):
                if rec["name"] == name:
                    match = cid
                    break
            if match is not None:
                del self._in_flight[match]
            self._push(kind="tool_result", tool=name, result=result)
        self._refresh_in_flight_strip()

    @QtCore.pyqtSlot(str)
    def _on_error(self, msg: str) -> None:
        # An error usually means the current turn has aborted — clear
        # the in-flight strip so it doesn't display stale calls.
        if self._in_flight:
            self._in_flight.clear()
            self._refresh_in_flight_strip()
        self._push(kind="error", text=msg)

    def _refresh_in_flight_strip(self) -> None:
        if not self._in_flight:
            if self._in_flight_lbl.isVisible():
                self._in_flight_lbl.setVisible(False)
                self._in_flight_lbl.setText("")
            return
        now = time.time()
        lines = []
        for rec in self._in_flight.values():
            elapsed = now - rec["started_ts"]
            m, s = divmod(int(elapsed), 60)
            timer = f"{m:02d}:{s:02d}"
            lines.append(f"⏱ {timer}   ► {rec['name']}  {rec['args_summary']}")
        self._in_flight_lbl.setText(
            f"<b>In flight ({len(self._in_flight)}):</b><br>"
            + "<br>".join(self._esc(l) for l in lines))
        self._in_flight_lbl.setVisible(True)

    # ── render + persist ───────────────────────────────────────────

    def _push(self, **event) -> None:
        event["ts"] = time.time()
        self._events.append(event)
        if len(self._events) > _MAX_EVENTS:
            self._events = self._events[-_MAX_EVENTS:]
        # Render only when this event passes the current filter
        if self._passes_filter(event):
            self._append_rendered(event)
        self._counts.setText(f"{len(self._events)} events")

    def _passes_filter(self, event: dict) -> bool:
        if not self._filter_tools_only:
            return True
        return event.get("kind") in ("tool_call", "tool_result", "error")

    def _append_rendered(self, event: dict) -> None:
        ts = datetime.fromtimestamp(event["ts"]).strftime("%H:%M:%S.%f")[:-3]
        kind = event.get("kind")
        if kind == "user":
            html = (f"<span style='color:{_COL_TIME}'>{ts}</span>  "
                    f"<span style='color:{_COL_USER}'><b>USER</b></span>  "
                    f"{self._esc(event.get('text',''))}")
        elif kind == "assistant":
            html = (f"<span style='color:{_COL_TIME}'>{ts}</span>  "
                    f"<span style='color:{_COL_ASSIST}'><b>ASSISTANT</b></span>  "
                    f"{self._esc(event.get('text',''))}")
        elif kind == "tool_call":
            args_str = _fmt_args(event.get("args") or {})
            html = (f"<span style='color:{_COL_TIME}'>{ts}</span>  "
                    f"<span style='color:{_COL_TOOL_CALL}'><b>► {self._esc(event.get('tool',''))}</b></span>  "
                    f"<span style='color:{_TEXT_DIM}'>{self._esc(args_str)}</span>")
        elif kind == "tool_result":
            result = event.get("result")
            is_err = (isinstance(result, dict) and
                      (result.get("error") or
                       (result.get("returncode") not in (None, 0))))
            marker_color = _COL_TOOL_ERR if is_err else _COL_TOOL_OK
            body = _fmt_result(result)
            html = (f"<span style='color:{_COL_TIME}'>{ts}</span>  "
                    f"<span style='color:{marker_color}'><b>◄ {self._esc(event.get('tool',''))}</b></span><br>"
                    f"<pre style='color:{_TEXT_MAIN}; margin:0 0 0 20px;'>{self._esc(body)}</pre>")
        elif kind == "error":
            html = (f"<span style='color:{_COL_TIME}'>{ts}</span>  "
                    f"<span style='color:{_COL_ERROR}'><b>ERROR</b></span>  "
                    f"{self._esc(event.get('text',''))}")
        else:
            html = self._esc(str(event))

        self._view.appendHtml(html)
        if self._auto_scroll:
            sb = self._view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _re_render_all(self) -> None:
        """Rebuild the display from the in-memory ring buffer (applies
        the current filter). Used when the filter toggle flips."""
        self._view.clear()
        for event in self._events:
            if self._passes_filter(event):
                self._append_rendered(event)

    @staticmethod
    def _esc(s: str) -> str:
        return (str(s).replace("&", "&amp;")
                       .replace("<", "&lt;")
                       .replace(">", "&gt;"))

    # ── control slots ──────────────────────────────────────────────

    def _on_filter_toggled(self, checked: bool) -> None:
        self._filter_tools_only = bool(checked)
        self._re_render_all()

    def _on_pause_toggled(self, checked: bool) -> None:
        self._auto_scroll = not bool(checked)

    def _clear(self) -> None:
        self._events.clear()
        self._view.clear()
        self._counts.setText("0 events")

    def _save_to_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Agent Console log", "agent_console.log",
            "Log files (*.log *.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                for e in self._events:
                    ts = datetime.fromtimestamp(e["ts"]).isoformat(timespec="milliseconds")
                    kind = e.get("kind", "?")
                    if kind == "user":
                        f.write(f"[{ts}] USER  {e.get('text','')}\n")
                    elif kind == "assistant":
                        f.write(f"[{ts}] ASSIST  {e.get('text','')}\n")
                    elif kind == "tool_call":
                        f.write(f"[{ts}] ► {e.get('tool','')}  {_fmt_args(e.get('args') or {})}\n")
                    elif kind == "tool_result":
                        f.write(f"[{ts}] ◄ {e.get('tool','')}\n{_fmt_result(e.get('result'))}\n")
                    elif kind == "error":
                        f.write(f"[{ts}] ERROR  {e.get('text','')}\n")
        except OSError as exc:
            QtWidgets.QMessageBox.warning(
                self, "Save failed", f"Could not write {path}:\n{exc}")
