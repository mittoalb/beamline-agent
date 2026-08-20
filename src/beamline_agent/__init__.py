"""beamline-agent — AI chat + sub-agent dispatch + status monitor +
image viewers for APS beamline tools.

Two public entry points:

    HostContext   — dataclass a host app fills to describe its
                    capabilities to the agent (main window, live-frame
                    grabber, beamline-plugin dispatcher, HDF5 viewer,
                    active beamline name, etc.). Missing capabilities
                    are handled gracefully — the agent's tool catalog
                    just doesn't include tools that would need them.

    mount(host)   — build the AI chat panel, wire the toolbar buttons
                    (📜 Console, 👥 Agents, 🎥 Task Rec), install the
                    docs bootstrap. Returns the built widgets for the
                    host to place in its layout.

Every module underneath (chat, console, panel, status, core_tools,
subagents, context, image_viewer, launcher_utils) is a self-contained
piece the host doesn't need to know about. Backward-compat re-exports
below keep `from beamline_agent import <name>` working for anything
callers used to import from `pystream.agent`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

LOGGER = logging.getLogger(__name__)


# ── HostContext — the plug the host fills in ────────────────────────

@dataclass
class HostContext:
    """Describes the capabilities a host app provides to the agent.

    Every field is optional except `main_window` — the agent has to
    know where to parent its dialogs. Missing capabilities cause the
    corresponding tools to be omitted from the catalog rather than
    crashing at call time.

    Fields (all optional except main_window):
      main_window       — the QMainWindow (or top-level QWidget) that
                          will host the chat dock + agent dialogs
      live_frame        — zero-arg callable returning the current
                          detector frame as a numpy array (or None).
                          When present, enables the `view_detector_image`
                          style tools that show what the beamline is
                          currently seeing.
      frame_signal      — a pyqtSignal(int, ndarray, float) or compatible
                          emitting on each new frame. Optional; used
                          by tools that need to correlate multiple
                          frames.
      active_beamline   — a string (e.g. "bl32ID") or None. Used by
                          the beamline-tool-catalog loader to fetch
                          provide_agent_context() from the right
                          beamline package.
      beamline_lookup   — callable(name: str) -> module | None. Given
                          a beamline name, returns the imported
                          beamline package. Defaults to importing
                          `pystream.beamlines.<name>` for backward
                          compat with the pystream host.
      open_hdf5_viewer  — callable(path: str, file_path: str = None)
                          that opens the host's HDF5 viewer on a file.
                          Enables the agent's `view_hdf5_file` tool.
      list_plugins      — zero-arg callable returning a list of dicts
                          describing available plugins (class_name,
                          button_text, group, handler_type, doc).
                          Enables `list_beamline_plugins`.
      open_plugin       — callable(name: str) -> str that opens a
                          beamline plugin dialog by class name or
                          button text. Enables `open_beamline_plugin`.
      user_home         — override for ~/.pystream (or wherever the
                          agent should store state). Defaults to
                          `~/.pystream` for pystream compat.
    """

    main_window:       Any                                = None
    live_frame:        Optional[Callable[[], Any]]        = None
    frame_signal:      Any                                = None
    active_beamline:   Optional[str]                      = None
    beamline_lookup:   Optional[Callable[[str], Any]]     = None
    open_hdf5_viewer:  Optional[Callable[..., Any]]       = None
    list_plugins:      Optional[Callable[[], list]]       = None
    open_plugin:       Optional[Callable[[str], str]]     = None
    user_home:         Optional[str]                      = None


# One process-global slot for the current host — modules that need
# host-provided callables (chat, core_tools, panel) read from here.
# Not thread-local because there's only ever one host per process.
_ACTIVE_HOST: Optional[HostContext] = None


def get_active_host() -> Optional[HostContext]:
    """Return the currently-mounted HostContext, or None if
    `mount()` hasn't been called yet. Used by internal modules to
    reach host-provided capabilities without threading the object
    through every function signature."""
    return _ACTIVE_HOST


# ── mount() — the entry point the host calls ────────────────────────

def mount(host: HostContext,
          persist_id: str = "dock",
          bootstrap_docs: bool = True) -> dict:
    """Build the agent UI + wire it into the host. Returns a dict of
    widget references the host can inspect / place in its layout::

        {
            "chat_widget":        AgentChatWidget,      — the AI panel
            "open_console":       callable,             — opens Console
            "open_agents_panel":  callable,             — opens Agents
            "open_task_recorder": callable,             — opens Task Rec
        }

    `persist_id` controls where chat history is saved (default "dock").
    `bootstrap_docs` copies packaged `.md` files into `~/.pystream/docs/`
    on first launch — set False in tests to avoid touching the FS.

    Idempotent: repeated calls with the same host return the same
    widget instances (won't spawn duplicate chat panels)."""
    global _ACTIVE_HOST

    if host is None:
        raise ValueError("host is required")
    if host.main_window is None:
        raise ValueError("HostContext.main_window is required — the "
                         "agent has to know where to parent dialogs")

    _ACTIVE_HOST = host

    if bootstrap_docs:
        try:
            from .context import bootstrap_agent_context_docs
            bootstrap_agent_context_docs()
        except Exception as e:
            LOGGER.warning("agent-context bootstrap failed: %s", e)

    # Build the chat widget (dock). Import here — the chat module
    # pulls in PyQt5, and mount() shouldn't force that dependency at
    # package-import time.
    from .chat import AgentChatWidget

    # Reuse an existing chat if a prior mount() already built one
    # on the same host — spares us duplicate dock panels.
    existing = getattr(host.main_window, "_beamline_agent_chat", None)
    if existing is not None:
        chat = existing
    else:
        chat = AgentChatWidget(parent=host.main_window,
                                persist_id=persist_id)
        host.main_window._beamline_agent_chat = chat

    def _open_console():
        from .console import AgentConsoleDialog
        dlg = getattr(host.main_window, "_agent_console_dialog", None)
        if dlg is None or not dlg.isVisible():
            dlg = AgentConsoleDialog(parent=host.main_window)
            host.main_window._agent_console_dialog = dlg
        else:
            dlg._wire_to_agents()
        dlg.show(); dlg.raise_(); dlg.activateWindow()
        return dlg

    def _open_agents_panel():
        from .panel import AgentsDialog
        dlg = getattr(host.main_window, "_agents_dialog", None)
        if dlg is None or not dlg.isVisible():
            dlg = AgentsDialog(parent=host.main_window)
            host.main_window._agents_dialog = dlg
        dlg.show(); dlg.raise_(); dlg.activateWindow()
        return dlg

    def _open_task_recorder():
        # Task recorder lives in pystream today (uses the pystream
        # viewer for frame grabs). Keep it there for the moment; the
        # host wires it in via its own toolbar button. This mount
        # entry stays as a placeholder for a future extraction pass.
        try:
            from pystream.task_recorder import TaskRecorderDialog
        except ImportError:
            LOGGER.warning("Task recorder not available (pystream not installed)")
            return None
        dlg = getattr(host.main_window, "_task_recorder_dialog", None)
        if dlg is None or not dlg.isVisible():
            dlg = TaskRecorderDialog(parent=host.main_window)
            host.main_window._task_recorder_dialog = dlg
        dlg.show(); dlg.raise_(); dlg.activateWindow()
        return dlg

    return {
        "chat_widget":        chat,
        "open_console":       _open_console,
        "open_agents_panel":  _open_agents_panel,
        "open_task_recorder": _open_task_recorder,
    }


# ── backward-compat re-exports (things imported from pystream.agent) ──

# These lazy-import so `from beamline_agent import <X>` works without
# forcing PyQt5 at package-load time for callers that only want the
# non-Qt bits (status, launcher_utils).

def __getattr__(name: str):
    """Lazy re-export table — kept out of the top-level namespace so
    the package can be imported cheaply."""
    _lazy_map = {
        # chat
        "AgentChatWidget":       ("chat", "AgentChatWidget"),
        "AgentDialog":           ("chat", "AgentDialog"),
        "DEFAULT_AGENT_NAME":    ("chat", "DEFAULT_AGENT_NAME"),
        "MAX_AGENT_ITERATIONS":  ("chat", "MAX_AGENT_ITERATIONS"),
        "PROTOCOL_ANTHROPIC":    ("chat", "PROTOCOL_ANTHROPIC"),
        "PROTOCOL_OPENAI":       ("chat", "PROTOCOL_OPENAI"),
        "PYSTREAM_HOME":         ("chat", "PYSTREAM_HOME"),
        "SYSTEM_PROMPT_DEFAULT": ("chat", "SYSTEM_PROMPT_DEFAULT"),
        "build_agent_panel":     ("chat", "build_agent_panel"),
        "load_settings":         ("chat", "load_settings"),
        "save_settings":         ("chat", "save_settings"),
        # console
        "AgentConsoleDialog":    ("console", "AgentConsoleDialog"),
        # context
        "bootstrap_agent_context_docs": ("context", "bootstrap_agent_context_docs"),
        # core_tools
        "CORE_SYSTEM_PROMPT_ADDENDUM": ("core_tools", "CORE_SYSTEM_PROMPT_ADDENDUM"),
        "CORE_TOOLS":            ("core_tools", "CORE_TOOLS"),
        "core_tool_context":     ("core_tools", "core_tool_context"),
        # panel
        "AgentsDialog":          ("panel", "AgentsDialog"),
        "AgentsPanel":           ("panel", "AgentsPanel"),
        "build_agents_panel":    ("panel", "build_agents_panel"),
        # status
        "AGENTS_FILE":           ("status", "AGENTS_FILE"),
        "APS_AGENTS_DIR":        ("status", "APS_AGENTS_DIR"),
        "AgentStatusPublisher":  ("status", "AgentStatusPublisher"),
        "child_env":             ("status", "child_env"),
        "load_registry":         ("status", "load_registry"),
        "purge_stale_records":   ("status", "purge_stale_records"),
        "update_record":         ("status", "update_record"),
    }
    if name in _lazy_map:
        module, attr = _lazy_map[name]
        import importlib
        mod = importlib.import_module(f".{module}", package=__name__)
        val = getattr(mod, attr)
        globals()[name] = val   # cache for future accesses
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "HostContext", "mount", "get_active_host",
    # re-exports (populated on demand by __getattr__)
    "AgentChatWidget", "AgentConsoleDialog", "AgentsDialog", "AgentsPanel",
    "AgentStatusPublisher", "CORE_SYSTEM_PROMPT_ADDENDUM", "CORE_TOOLS",
    "MAX_AGENT_ITERATIONS", "PYSTREAM_HOME", "bootstrap_agent_context_docs",
    "build_agent_panel", "build_agents_panel", "child_env",
    "core_tool_context", "load_registry", "load_settings",
    "purge_stale_records", "save_settings", "update_record",
]
