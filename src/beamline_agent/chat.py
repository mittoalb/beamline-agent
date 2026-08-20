"""
AI Agent — pystream core (beamline-agnostic).

Chat panel + settings dialog that talks to a Gateway speaking either
the Anthropic Messages API protocol or the OpenAI Chat Completions
protocol. Runs in a background QThread so the GUI stays responsive.

The chat itself, the transcript, the ⚙ settings, the {name} /
{beamline} substitutions, prompt caching, and history persistence are
all universal — no beamline knowledge required.

Beamline-specific tools + prompt-body live in the active beamline's
package (see e.g. `pystream/beamlines/bl32ID/agent_tools.py`). Each
beamline optionally exports `provide_agent_context()` from its
`__init__.py` — pystream queries it at every Send. Missing hook =
tool-less pure-chat agent. Empty beamline = still works.

Configuration + history persist under ~/.pystream/ :
    agent_settings.json          gateway URL/key/model/name
    agent_history_dock.json      dock conversation transcript
"""

import json
import logging
import os
import re
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import pyqtSignal

LOGGER = logging.getLogger(__name__)


# ── Storage locations ──────────────────────────────────────────────────

PYSTREAM_HOME = os.path.expanduser("~/.pystream")
AGENT_SETTINGS_FILE = os.path.join(PYSTREAM_HOME, "agent_settings.json")
# Legacy path we migrate from on first load. bl32ID's plugin_settings
# used to nest agent config under an "AgentDialog" key here.
_LEGACY_BL32ID_SETTINGS_FILE = os.path.join(PYSTREAM_HOME, "bl32ID_settings.json")


def load_settings() -> dict:
    """Universal agent settings loader. Migrates from bl32ID_settings
    on first call if the new file doesn't exist yet."""
    try:
        with open(AGENT_SETTINGS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # One-time migration from the old bl32ID-nested location.
    try:
        with open(_LEGACY_BL32ID_SETTINGS_FILE) as f:
            legacy = json.load(f)
        if isinstance(legacy, dict) and isinstance(legacy.get("AgentDialog"), dict):
            migrated = legacy["AgentDialog"]
            save_settings(migrated)
            return migrated
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def save_settings(cfg: dict):
    try:
        os.makedirs(PYSTREAM_HOME, exist_ok=True)
        with open(AGENT_SETTINGS_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# ── Beamline tool-context lookup ──────────────────────────────────────

# Shape returned by a beamline's `provide_agent_context()` (all optional):
#   {
#     "tool_specs_anthropic":  list of dicts (Anthropic tool defs),
#     "tool_specs_openai":     list of dicts (OpenAI tool defs),
#     "get_tool":              callable(name) → callable | None,
#     "write_tools":           set of str (names needing confirmation),
#     "is_destructive":        callable(bash_cmd) → bool,
#     "system_prompt_addendum": str (appended after the core prompt),
#   }
# Any missing key → treated as empty / no tools. A completely missing
# hook → no tools, no addendum — agent runs as a pure chat.

_EMPTY_TOOL_CONTEXT = {
    "tool_specs_anthropic":   [],
    "tool_specs_openai":      [],
    "get_tool":               lambda name: None,
    "write_tools":            set(),
    "is_destructive":         lambda cmd: False,
    "system_prompt_addendum": "",
}


def _active_beamline_module():
    """Return the active beamline package (as a module object), or
    None. Three sources tried in order:

    1. The host-provided `beamline_lookup` callback (preferred — a
       host can point at any beamline package).
    2. The host-provided `active_beamline` name + default lookup at
       `pystream.beamlines.<name>` (pystream compat).
    3. Fallback: import `pystream.beamline_config.ACTIVE_BEAMLINE` and
       resolve to `pystream.beamlines.<name>` (works when the host
       hasn't yet set a HostContext, e.g. tests)."""
    try:
        from . import get_active_host
        host = get_active_host()
    except Exception:
        host = None

    active_name = None
    if host is not None:
        if host.beamline_lookup and host.active_beamline:
            try:
                mod = host.beamline_lookup(host.active_beamline)
                if mod is not None:
                    return mod
            except Exception:
                pass
        active_name = host.active_beamline

    if active_name is None:
        try:
            from pystream.beamline_config import ACTIVE_BEAMLINE  # type: ignore
            active_name = ACTIVE_BEAMLINE
        except Exception:
            return None

    if not active_name:
        return None
    try:
        import importlib
        return importlib.import_module(
            f"pystream.beamlines.{active_name}")
    except Exception:
        return None


def _load_tool_context() -> dict:
    """Fetch the effective tool context for a Send. Always includes the
    core, beamline-agnostic tools from `agent_core_tools`; merges the
    active beamline's `provide_agent_context()` on top when present.

    Merge semantics:
      - tool_specs_* : core specs + beamline specs (both visible to model)
      - get_tool     : beamline first, core fallback (beamline can
                       override a core tool of the same name)
      - write_tools  : union (any tool either side flags is confirmed)
      - is_destructive: OR (either check trips the confirmation gate)
      - system_prompt_addendum: core text + beamline text, in that order
    """
    from .core_tools import core_tool_context

    core = core_tool_context()

    bl = dict(_EMPTY_TOOL_CONTEXT)
    mod = _active_beamline_module()
    if mod is not None:
        hook = getattr(mod, "provide_agent_context", None)
        if callable(hook):
            try:
                ctx = hook() or {}
                for k, v in ctx.items():
                    if v is not None:
                        bl[k] = v
            except Exception:
                pass  # beamline hook failed — fall back to empty overlay

    core_get = core["get_tool"]
    bl_get   = bl["get_tool"]
    core_dst = core["is_destructive"]
    bl_dst   = bl["is_destructive"]

    addendum_parts = [
        (core["system_prompt_addendum"] or "").strip(),
        (bl["system_prompt_addendum"] or "").strip(),
    ]
    merged_addendum = "\n\n".join(p for p in addendum_parts if p)

    return {
        "tool_specs_anthropic": list(core["tool_specs_anthropic"])
                                + list(bl["tool_specs_anthropic"]),
        "tool_specs_openai":    list(core["tool_specs_openai"])
                                + list(bl["tool_specs_openai"]),
        "get_tool":             lambda name: bl_get(name) or core_get(name),
        "write_tools":          set(core["write_tools"]) | set(bl["write_tools"]),
        "is_destructive":       lambda cmd: bool(bl_dst(cmd)) or bool(core_dst(cmd)),
        "system_prompt_addendum": merged_addendum,
    }


# ── confirmation bridge (worker thread → GUI thread) ────────────────────

class _ConfirmHelper(QtCore.QObject):
    """Lives on the GUI thread. Worker threads call its `ask` slot via
    BlockingQueuedConnection to pop a Yes/No QMessageBox and get the
    user's answer back. Used to gate write-class tools."""

    @QtCore.pyqtSlot(str, str, result=bool)
    def ask(self, title: str, message: str) -> bool:
        reply = QtWidgets.QMessageBox.question(
            None, title, message,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return reply == QtWidgets.QMessageBox.Yes


class _GuiActionHelper(QtCore.QObject):
    """Lives on the GUI thread. Worker threads call its slots via
    BlockingQueuedConnection to trigger GUI-side actions from a tool
    call — opening the HDF5 viewer, listing / opening beamline
    plugins."""

    def _find_main(self):
        """Walk up the parent chain to find the pystream main window
        (identified by having `_open_viewer` / `_open_beamline_plugin`).
        Returns None if unreachable."""
        widget = self.parent()
        while widget is not None:
            if hasattr(widget, "_open_viewer") and hasattr(widget, "_open_beamline_plugin"):
                return widget
            widget = widget.parent()
        return None

    @QtCore.pyqtSlot(str, result=str)
    def open_hdf5_viewer(self, path: str) -> str:
        """Find the pystream main window, ask it to open the HDF5
        viewer on `path`. Returns "" on success, or an error message."""
        try:
            if not path:
                return "empty path"
            if not os.path.isfile(os.path.expanduser(path)):
                return f"file does not exist: {path}"
            main = self._find_main()
            if main is None:
                return "pystream main window not reachable"
            main._open_viewer(file_path=os.path.expanduser(path))
            return ""
        except Exception as ex:  # noqa: BLE001 — must return a string
            return f"{type(ex).__name__}: {ex}"

    @QtCore.pyqtSlot(result=str)
    def list_beamline_plugins_json(self) -> str:
        """Return a JSON string of the active beamline's plugin list."""
        import json as _json
        main = self._find_main()
        if main is None:
            return _json.dumps({"error": "pystream main window not reachable"})
        try:
            return _json.dumps({"plugins": main._list_beamline_plugins()})
        except Exception as ex:
            return _json.dumps({"error": f"{type(ex).__name__}: {ex}"})

    @QtCore.pyqtSlot(str, result=str)
    def open_beamline_plugin(self, name: str) -> str:
        """Ask the main window to open a plugin by class-name or
        BUTTON_TEXT. Returns "" on success or an error string."""
        main = self._find_main()
        if main is None:
            return "pystream main window not reachable"
        try:
            return main._open_beamline_plugin(name)
        except Exception as ex:
            return f"{type(ex).__name__}: {ex}"

    @QtCore.pyqtSlot(str, result=str)
    def open_image_viewer(self, path: str) -> str:
        """Open the agent-only image viewer on `path`. Returns "" on
        success or an error message. The dialog is parented to the
        main window so it cleans up when pystream exits."""
        try:
            if not path:
                return "empty path"
            path = os.path.expanduser(path)
            if not os.path.isfile(path):
                return f"file does not exist: {path}"
            main = self._find_main()
            if main is None:
                return "pystream main window not reachable"
            from .image_viewer import open_image_viewer
            # Keep a reference on the main window so the dialog survives
            # scope exit. A list, so multiple images can coexist.
            store = getattr(main, "_agent_image_viewers", None)
            if store is None:
                store = []
                main._agent_image_viewers = store
            # Prune dialogs the user has closed
            store[:] = [d for d in store if d.isVisible()]
            dlg = open_image_viewer(path, parent=main)
            store.append(dlg)
            return ""
        except Exception as ex:
            return f"{type(ex).__name__}: {ex}"


def _confirmation_message(name: str, args: dict) -> str:
    """Format a clear, scannable confirmation message per write tool."""
    if name == "bash":
        cmd = args.get("command", "?")
        return (
            f"The agent wants to run a shell command:\n\n"
            f"  {cmd}\n\n"
            f"This was flagged as potentially destructive. Run it?"
        )
    if name == "caput":
        pv = args.get("pv_name", "?")
        val = args.get("value", "?")
        return (
            f"The agent wants to write to an EPICS PV:\n\n"
            f"  {pv}  ←  {val!r}\n\n"
            f"Allow?"
        )
    return (
        f"The agent wants to call:\n\n"
        f"  {name}({json.dumps(args, default=str)})\n\n"
        f"Allow this?"
    )


def _needs_confirmation(name: str, arguments: dict, tool_ctx: dict) -> bool:
    """True if this tool call should pop the Yes/No dialog. Static for
    write tools; dynamic for bash (only destructive commands gate).
    `tool_ctx` supplies the write-set and the bash-heuristic from the
    active beamline."""
    if name in tool_ctx.get("write_tools", set()):
        return True
    if name == "bash":
        is_destructive = tool_ctx.get("is_destructive", lambda _c: False)
        return bool(is_destructive(arguments.get("command", "")))
    return False


PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_OPENAI = "openai"


# ── backend presets ────────────────────────────────────────────────────
# The Settings dialog exposes a "Backend" choice on top of the raw
# `protocol` so a user doesn't have to memorise gateway URLs. The
# preset controls (a) which `protocol` is used, (b) the URL
# placeholder shown in the dialog, and (c) whether an API key is
# expected. The wire protocol under the hood is unchanged: Local
# rides `PROTOCOL_OPENAI` because every popular local runner
# (Ollama, llama.cpp, vLLM, LM Studio) exposes an OpenAI-compatible
# `/v1` endpoint — that's the whole reason a bespoke "local
# protocol" isn't needed.
BACKEND_ANTHROPIC = "anthropic-cloud"
BACKEND_OPENAI    = "openai-cloud"
BACKEND_LOCAL     = "local-openai"
BACKEND_CUSTOM    = "custom"

# Ordered list of (backend_id, display_label, protocol, url_placeholder,
# needs_key). The dialog builds its combobox from this table.
BACKEND_PRESETS = [
    (BACKEND_ANTHROPIC, "Anthropic (cloud)", PROTOCOL_ANTHROPIC,
     "https://gateway.example.com (Anthropic Messages — no /v1)", True),
    (BACKEND_OPENAI,    "OpenAI (cloud)",    PROTOCOL_OPENAI,
     "https://gateway.example.com/v1 (OpenAI)", True),
    (BACKEND_LOCAL,     "Local — OpenAI-compatible", PROTOCOL_OPENAI,
     "http://<host>:<port>/v1  (Ollama 11434, llama.cpp 8080, vLLM 8000, "
     "LM Studio 1234)", False),
    (BACKEND_CUSTOM,    "Custom",            PROTOCOL_ANTHROPIC,
     "https://your-endpoint.example.com/…", True),
]

# Sentinel key sent to the openai client when the user leaves the
# API-key field blank — required because the client refuses to
# instantiate on an empty key even when the target server (a local
# runner) doesn't care.
_LOCAL_KEY_SENTINEL = "local-no-key"


def _resolve_api_key(api_key: str | None) -> str:
    """Return the api_key to actually send. Empty / None becomes the
    local-runner sentinel so the openai client instantiates cleanly."""
    k = (api_key or "").strip()
    return k or _LOCAL_KEY_SENTINEL


def _backend_from_settings(s: dict) -> str:
    """Pick the backend id from a settings dict. Explicit `backend`
    wins; otherwise derive from `protocol` (backward-compat for
    settings files written before the backend field existed)."""
    if not isinstance(s, dict):
        return BACKEND_ANTHROPIC
    b = (s.get("backend") or "").strip()
    if b in {p[0] for p in BACKEND_PRESETS}:
        return b
    proto = s.get("protocol") or PROTOCOL_ANTHROPIC
    if proto == PROTOCOL_ANTHROPIC:
        return BACKEND_ANTHROPIC
    return BACKEND_OPENAI

# Cap on agentic iterations per turn — protects against runaway tool loops.
# Kept intentionally tight (10) because a well-structured task needs few
# rounds: read the relevant AGENTS.md ONCE, run the CLI ONCE, inspect the
# result ONCE. If the agent hits this cap it's burning rounds on
# non-productive verification (repeated filesystem searches, redundant
# --help calls, re-reading docs). The fix is a sharper prompt / AGENTS.md,
# NOT a higher cap.
MAX_AGENT_ITERATIONS = 10


DEFAULT_AGENT_NAME = "Röntgen"


def _active_beamline() -> str:
    """Return the currently-active beamline name for prompt
    substitution ('bl32ID', 'bl19BM', ...). Prefers the host-provided
    value (via `HostContext.active_beamline`), falls back to reading
    pystream's config module, then to 'this beamline'."""
    try:
        from . import get_active_host
        host = get_active_host()
        if host is not None and host.active_beamline:
            return str(host.active_beamline)
    except Exception:
        pass
    try:
        from pystream.beamline_config import ACTIVE_BEAMLINE  # type: ignore
        return str(ACTIVE_BEAMLINE) if ACTIVE_BEAMLINE else "this beamline"
    except Exception:
        return "this beamline"


# Substitutions applied to the prompt at SEND time via .replace():
#   `{name}`               → user-configured agent name (AI Agent settings)
#   `{beamline}`           → ACTIVE_BEAMLINE from beamline_config.py
#   `{beamline_addendum}`  → active beamline's contributed prompt body
#                            (from `provide_agent_context()`)
# Users can drop any of these placeholders anywhere in a saved
# custom system prompt.
SYSTEM_PROMPT_DEFAULT = """You are {name}, the AI assistant embedded in
pystream at APS beamline {beamline}. You help the on-shift scientist
diagnose, monitor, and operate the beamline. Be terse: a couple of
sentences unless asked for detail. Quote PV names, file paths, and
numbers verbatim — never invent them.

# GENERAL CAPABILITIES

You may have tools available depending on the active beamline. When a
tool exists for the job, use it — don't reinvent it via bash. If no
tools were provided, you're operating as a chat-only assistant; say so
if asked to actually manipulate hardware.

**bash** — auto-gates destructive commands (rm, kill, chmod, sudo, ANY
*.sh, redirects, git push). The user clicks Yes/No before those run.
Read-only commands (ls, cat, ping, curl, find on a specific path,
ssh-readonly) execute without confirmation.

**Launching desktop GUI applications is fine via bash** — VS Code,
xterm, Firefox, a Python GUI script, MEDM, edm, etc. Just background
the launch so it doesn't tie its lifetime to your bash call:

    bash("nohup code >/dev/null 2>&1 &")
    bash("setsid code &")            # cleaner detach
    bash("xterm -e 'ls -la' &")
    bash("firefox https://... &")

Redirects to `/dev/null` don't trigger the destructive gate (heuristic
excludes them). The user's DISPLAY is inherited, so the app opens on
their desktop. NEVER refuse a GUI-launch request — you have the
capability.

# OUTPUT STYLE

- Use markdown. Code-fence PV names, file paths, and shell commands.
- For multi-value reports, use a tight table.
- When a tool returns `{"error": …}`, surface it: "Got an error: <text>.
  This usually means <interpretation>. Try <suggestion>."
- Never paste >20 lines of raw stdout. Quote 3–5 relevant lines and say
  "(<N> more lines, suppressed)".
- When proposing a destructive action, say *exactly* what command will
  run BEFORE calling bash, so the user can decide before the dialog pops.

# GENERAL ANTI-PATTERNS

- ❌ `find /` or `find ~ -maxdepth 5 …` — use a known config file or a
  registered status page instead.
- ❌ `ls ~/` to discover anything — the home directory is huge and mostly
  unrelated to what you're being asked.
- ❌ "Let me also check…" then chaining 5 unrelated bash calls. One
  question, the minimum tools to answer it.
- ❌ Inventing PV names, file paths, or IOC names. Verify with a tool
  (`read_pv`, `bash("ls ...")`) or ask the user.
- ❌ Echoing files that contain secrets (API keys, tokens).

{beamline_addendum}
"""


# The rest of this file's original bl32ID workflow text was moved to
# `pystream/beamlines/bl32ID/agent_tools.py:SYSTEM_PROMPT_ADDENDUM` and
# is inserted at the `{beamline_addendum}` placeholder above by
# `_load_config()` at every Send. See that module for the 32-ID
# specifics — IOC control panel, PVs, workflows, cheat sheet.



# ── tool dispatch helper ────────────────────────────────────────────────

def _execute_tool(name: str, arguments: dict, tool_ctx: dict, confirm=None) -> dict:
    """Run a tool by name with the model-provided arguments. Always
    returns a JSON-serializable dict — tools wrap their own exceptions.

    `tool_ctx` is the active beamline's contribution: get_tool,
    write_tools, is_destructive. If a beamline provides no tools, every
    call falls through to `unknown tool`.

    If the tool needs confirmation, `confirm(title, message) -> bool` is
    called first. Missing confirm callback → refuses to run write tools."""
    func = tool_ctx.get("get_tool", lambda _n: None)(name)
    if func is None:
        return {"error": f"unknown tool: {name}"}
    if not isinstance(arguments, dict):
        return {"error": f"arguments must be an object, got {type(arguments).__name__}"}

    if _needs_confirmation(name, arguments, tool_ctx):
        if confirm is None:
            return {"error": f"{name} requires user confirmation but no "
                             f"confirmation channel is available — refusing."}
        approved = False
        try:
            approved = bool(confirm("Confirm action", _confirmation_message(name, arguments)))
        except Exception as ex:
            return {"error": f"confirmation prompt failed: {ex}"}
        if not approved:
            return {"error": "user denied the action",
                    "denied": True, "tool": name, "arguments": arguments}

    try:
        result = func(**arguments)
    except TypeError as ex:
        return {"error": f"bad arguments to {name}: {ex}"}
    except Exception as ex:
        return {"error": f"{type(ex).__name__} in {name}: {ex}"}
    return result if isinstance(result, dict) else {"value": result}


def _anthropic_tool_result_content(result):
    """If a tool result carries an embedded image (`image_base64` +
    `media_type`), package it as a real Anthropic content list with an
    image block + text block of the remaining metadata. Otherwise return
    plain JSON text. Anthropic's vision-capable models will SEE the image."""
    if isinstance(result, dict) and result.get("image_base64"):
        img_b64 = result["image_base64"]
        media_type = result.get("media_type", "image/png")
        text_payload = {k: v for k, v in result.items()
                        if k not in ("image_base64", "media_type")}
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": img_b64,
                },
            },
            {"type": "text",
             "text": json.dumps(text_payload, default=str)},
        ]
    return json.dumps(result, default=str)


def _openai_tool_result_text(result):
    """OpenAI's tool-role messages are text-only — strip any embedded
    base64 image (the model can't decode it as text), keep the metadata."""
    if isinstance(result, dict) and result.get("image_base64"):
        clone = {k: v for k, v in result.items() if k != "image_base64"}
        clone["_note"] = ("image data omitted — OpenAI tool-result channel "
                          "is text-only. Switch to an Anthropic Gateway "
                          "model to see the actual image.")
        return json.dumps(clone, default=str)
    return json.dumps(result, default=str)


# ── hallucination detector ─────────────────────────────────────────────
#
# LIVE INCIDENT 2026-08-20: on repeated motor-move requests, models
# were returning text-only replies ("Both at 1.0000, DMOV=1", "caput
# fired", "verified via RBV") without emitting ANY tool_call in the
# turn. The user saw no confirmation dialog, the motors didn't move,
# and the transcript filled with fake successes. This detector runs
# at the point the loop would exit, catches the fabrication, and
# forces one more iteration with a corrective message telling the
# model to actually invoke the tool.
#
# Two conditions must be true:
#   1. The USER's original message asks for an ACTION (imperative
#      verb like move / set / put / caput / write / run / fire).
#   2. The MODEL's reply CLAIMS an action (past-tense verb like
#      moved / fired / wrote / set / caput'd / done, or a
#      verification claim like DMOV=1 / RBV=N / verified).
# AND: the whole turn's tool_call count is zero.
#
# The correction message tells the model, plainly, that its previous
# reply was hallucinated. We allow up to `HALLUCINATION_MAX_RETRIES`
# corrections per turn; beyond that we surface a hard error rather
# than let the model spin.

HALLUCINATION_MAX_RETRIES = 2

_USER_ACTION_RE = re.compile(
    r"\b("
    r"move|set|put|go(?:\s+to)?|caput|write|run|fire|send|drive|"
    r"restart|open|close|start|stop|trigger|reset|"
    r"back\s+to|to\s+[\-+0-9]"
    r")\b",
    re.IGNORECASE,
)

# Weak claims — past-tense verbs that could appear in a legit reply
# ("moved successfully to 1.0" AFTER a real caput). Require a numeric
# value or explicit completion phrase so we don't false-positive on
# educational replies like "you call caput to set a PV".
#
# Each alternation handles its own boundaries — a wrapping
# `\b(...)\b` would break `done.` (the trailing `.` is non-word, so
# `\b` after doesn't fire at end of string). Live incident 2026-08-20:
# reply "From txm4: ZP X = 1.0. Done." wasn't detected because of that.
_MODEL_CLAIM_WEAK_RE = re.compile(
    r"("
    r"\bmoved\s+to\s+[\-+0-9]|"
    r"\bset\s+to\s+[\-+0-9]|"
    r"\b(?:already|now)\s+at\s+[\-+0-9.eE]+|"
    r"\bdone\b|"
    r"\bexecuted\b|"
    r"\bcomplete(?:d)?\b"
    r")",
    re.IGNORECASE,
)

# Strong claims — phrases the model only produces when it's
# fabricating a tool-call report. These trigger detection ALONE,
# without needing an action verb in the user's message. Legit
# replies never contain these unless a tool actually ran.
_MODEL_CLAIM_STRONG_RE = re.compile(
    r"("
    r"\bcaput(?:s|ed|ted|'d)?\s+(?:fired|firing|written|sent|"
    r"succeeded|OK|done|this\s+turn)|"
    r"\bfired?\s+(?:the\s+)?caput|"
    r"\bfired\s+both\b|"
    r"\bfiring\s+caputs?|"
    r"\bwrote\s+to\b|"
    r"\bverified\s+(?:via|after|with)\s+(?:rbv|caput)|"
    r"\bverified\s+by\s+rbv|"
    r"\brbv\s+verified|"
    r"\bdmov\s*=\s*1|"
    r"\brbv\s*=|"
    r"\battempts?\s+succeeded|"
    r"\bmove\s+(?:issued|fired|committed)|"
    r"\bboth\s+at\s+[\-+0-9]|"
    # Infrastructure fabrication — hostnames, gateways, IPs,
    # architecture claims the model invents to explain away a
    # missing tool call. Live incident 2026-08-20: "Host:
    # `hulk.aps.anl.gov`. EPICS gateway: `s32dserv2.xray.aps.anl.gov:5064`"
    # was reported without any `bash: hostname` call.
    r"\b(?:host|hostname)\s*(?::|is|=)\s*[a-z0-9][a-z0-9.\-]*|"
    r"\bepics[\s_]*gateway\s*(?::|=)|"
    r"\bca[\s_]*gateway\s*(?::|=)|"
    r"\b(?:runs?|running)\s+on\s+[a-z][a-z0-9.\-]*(?:\.aps\.anl\.gov)?|"
    r"\bsshes?\s+(?:in)?to\s+[a-z]|"
    r"\bsplit\s+architecture|"
    r"\bagent\s+(?:runtime|backend)\s+(?:on|is\s+on)\s+[a-z]|"
    # "From <host>: …" pattern — model dressing up a fabricated
    # result as if it came from a specific machine.
    r"\bfrom\s+(?:txm[0-9]+|tomo[0-9]+|gauss|hulk|beams|s32[a-z0-9]*):"
    r")",
    re.IGNORECASE,
)


def _is_action_hallucination(user_text: str,
                              reply_text: str,
                              tools_called_this_turn: int) -> bool:
    """Return True when the model returned an action-claim reply
    without having invoked any tool this turn.

    Detection has two tiers:
      * STRONG claim — the reply contains a phrase the model only
        produces when fabricating a tool-call report ("caput fired",
        "DMOV=1", "verified via RBV", "Both at 1.0"). Triggers alone.
      * WEAK claim — a general past-tense verb ("moved to 1", "done")
        that could appear in a legit reply. Requires the user's own
        message to have implied an action too."""
    if tools_called_this_turn > 0:
        return False
    if not reply_text:
        return False
    # Strong claims trigger regardless of the user's phrasing —
    # legit replies to non-action questions don't contain them.
    if _MODEL_CLAIM_STRONG_RE.search(reply_text):
        return True
    # Weak claims need a matching user-side action verb.
    if not _USER_ACTION_RE.search(user_text or ""):
        return False
    return bool(_MODEL_CLAIM_WEAK_RE.search(reply_text))


_HALLUCINATION_CORRECTION = (
    "AGENT-LOOP GUARD: Your previous reply claims an action was "
    "performed (\"{claim_snippet}\") but you did NOT invoke any "
    "tool this turn. Text-only replies do nothing on this system — "
    "the beamline / motor / plot / file only changes when you emit "
    "a tool_call. This is not a permission or config problem; it "
    "is a hallucination pattern seen in your reply. RETRY NOW: "
    "call the appropriate tool (caput / bash / open_beamline_plugin "
    "/ etc.) BEFORE writing any text. Do not include the words "
    "\"done\", \"fired\", \"verified\", or similar until AFTER you "
    "receive a tool_result confirming success. If your claim was "
    "about infrastructure (hostname, host, EPICS gateway, "
    "\"runs on X\", \"SSHes to Y\"), you fabricated it — the only "
    "way to know a hostname is `bash: hostname`, an EPICS gateway "
    "is `bash: echo $EPICS_CA_ADDR_LIST`, etc. Call the tool."
)

_HALLUCINATION_BANNER = (
    "⚠️ AGENT-LOOP GUARD: the reply below was flagged as a "
    "hallucination — the model claimed an action but did not "
    "actually invoke any tool. The claim may be false. Nothing "
    "was written to the beamline, no file was produced, no PV "
    "was changed. Re-issue your request or check the Console for "
    "actual tool activity this turn."
)


def _hallucination_snippet(reply_text: str, limit: int = 80) -> str:
    """Trim a reply for the correction message so we don't feed the
    whole hallucinated paragraph back to the model."""
    t = (reply_text or "").strip().replace("\n", " ")
    if len(t) <= limit:
        return t
    return t[:limit] + "…"


# ── chat: Anthropic protocol ────────────────────────────────────────────

def _chat_anthropic(base_url, api_key, model, system_prompt,
                    history, user_text, emit_tool, confirm, tool_ctx):
    """Agentic loop on the Anthropic Messages API. `emit_tool` is a callback
    `(name, arguments, result_or_None) -> None` invoked once at call-start
    (result=None) and once at completion."""
    import anthropic
    client = anthropic.Anthropic(base_url=base_url, api_key=api_key,
                                 timeout=60.0, max_retries=2)
    tools = tool_ctx.get("tool_specs_anthropic", []) or []
    messages = [*history, {"role": "user", "content": user_text}]

    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    tools_called_this_turn = 0    # for the hallucination detector
    hallucination_retries = 0

    for _ in range(MAX_AGENT_ITERATIONS):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=tools,
            messages=messages,
        )
        u = response.usage
        totals["input"] += getattr(u, "input_tokens", 0) or 0
        totals["output"] += getattr(u, "output_tokens", 0) or 0
        totals["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        totals["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason != "tool_use":
            text = "".join(
                b.text for b in response.content
                if getattr(b, "type", None) == "text"
            ).strip()

            # Hallucination guard — see the section-header comment above
            # `_is_action_hallucination` for the incident this fixes.
            if _is_action_hallucination(
                    user_text, text, tools_called_this_turn):
                if hallucination_retries < HALLUCINATION_MAX_RETRIES:
                    hallucination_retries += 1
                    LOGGER.warning(
                        "hallucinated action reply (retry %d/%d): %r",
                        hallucination_retries, HALLUCINATION_MAX_RETRIES,
                        text[:120])
                    # Feed the model back a user-role correction. Anthropic
                    # models handle a plain user message here; the previous
                    # assistant text stays in-context so the model can see
                    # what it claimed vs. what it should have done.
                    messages.append({"role": "assistant",
                                      "content": response.content})
                    messages.append({"role": "user", "content":
                        _HALLUCINATION_CORRECTION.format(
                            claim_snippet=_hallucination_snippet(text))})
                    continue
                # Retries exhausted — prepend a clear warning so the
                # user sees "this reply is fabricated" instead of
                # trusting the fake success message.
                LOGGER.error("hallucination guard exhausted; delivering "
                             "reply with warning banner: %r", text[:120])
                return _HALLUCINATION_BANNER + "\n\n" + text, totals

            return text, totals

        # Append the assistant's content (text + tool_use blocks) verbatim,
        # then run each tool and feed results back as a user turn.
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for b in response.content:
            if getattr(b, "type", None) == "tool_use":
                tools_called_this_turn += 1
                emit_tool(b.name, b.input, None)
                result = _execute_tool(b.name, b.input, tool_ctx, confirm=confirm)
                emit_tool(b.name, b.input, result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": _anthropic_tool_result_content(result),
                })
        messages.append({"role": "user", "content": tool_results})

    return "(stopped: hit MAX_AGENT_ITERATIONS — too many tool calls)", totals


# ── chat: OpenAI protocol ───────────────────────────────────────────────

def _chat_openai(base_url, api_key, model, system_prompt,
                 history, user_text, emit_tool, confirm, tool_ctx):
    """Agentic loop on OpenAI Chat Completions.

    ``api_key`` may be empty when talking to a local OpenAI-compatible
    server (Ollama, llama.cpp, vLLM, LM Studio). We substitute a
    sentinel so the openai client instantiates; the local server
    ignores it. Cloud gateways still see whatever real key was given."""
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=_resolve_api_key(api_key),
                    timeout=60.0, max_retries=2)
    tools = tool_ctx.get("tool_specs_openai", []) or []
    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_text},
    ]
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    tools_called_this_turn = 0
    hallucination_retries = 0

    for _ in range(MAX_AGENT_ITERATIONS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools, max_tokens=4096,
        )
        u = response.usage
        totals["input"] += getattr(u, "prompt_tokens", 0) or 0
        totals["output"] += getattr(u, "completion_tokens", 0) or 0
        cd = getattr(u, "prompt_tokens_details", None)
        totals["cache_read"] += (getattr(cd, "cached_tokens", 0) or 0) if cd else 0

        msg = response.choices[0].message
        if not msg.tool_calls:
            text = (msg.content or "").strip()
            # Hallucination guard — mirror of the Anthropic path above.
            if _is_action_hallucination(
                    user_text, text, tools_called_this_turn):
                if hallucination_retries < HALLUCINATION_MAX_RETRIES:
                    hallucination_retries += 1
                    LOGGER.warning(
                        "hallucinated action reply (retry %d/%d): %r",
                        hallucination_retries, HALLUCINATION_MAX_RETRIES,
                        text[:120])
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content":
                        _HALLUCINATION_CORRECTION.format(
                            claim_snippet=_hallucination_snippet(text))})
                    continue
                LOGGER.error("hallucination guard exhausted; delivering "
                             "reply with warning banner: %r", text[:120])
                return _HALLUCINATION_BANNER + "\n\n" + text, totals
            return text, totals

        # Re-attach the assistant turn including its tool_calls, then send
        # one tool message per call.
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [{
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            } for tc in msg.tool_calls],
        })
        tools_called_this_turn += len(msg.tool_calls)
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            emit_tool(tc.function.name, args, None)
            result = _execute_tool(tc.function.name, args, tool_ctx, confirm=confirm)
            emit_tool(tc.function.name, args, result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _openai_tool_result_text(result),
            })

    return "(stopped: hit MAX_AGENT_ITERATIONS — too many tool calls)", totals


# ── worker thread ───────────────────────────────────────────────────────

class _ChatWorker(QtCore.QThread):
    """Throwaway worker — one chat turn per instance. Emits tool_event for
    every tool call so the dialog can render it live."""

    done = pyqtSignal(str, dict)            # (assistant_text, usage_dict)
    error = pyqtSignal(str)
    tool_event = pyqtSignal(str, dict, object)  # (name, args, result-or-None)

    def __init__(self, protocol, base_url, api_key, model,
                 system_prompt, history, user_text,
                 tool_ctx, confirm_helper=None, gui_action_helper=None):
        super().__init__()
        self.protocol = protocol
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.history = history
        self.user_text = user_text
        self.tool_ctx = tool_ctx or dict(_EMPTY_TOOL_CONTEXT)
        self.confirm_helper = confirm_helper
        # Kept as `self._gui_action_helper` so the run() code that
        # reads it doesn't need refactoring; also matches the widget
        # attribute name to keep grep-ability.
        self._gui_action_helper = gui_action_helper

    def _emit_tool(self, name, args, result):
        self.tool_event.emit(name, dict(args), result)

    def _confirm(self, title, message) -> bool:
        """Block this worker thread, ask the GUI thread for Yes/No."""
        if self.confirm_helper is None:
            return False
        result = QtCore.QMetaObject.invokeMethod(
            self.confirm_helper, "ask",
            QtCore.Qt.BlockingQueuedConnection,
            QtCore.Q_RETURN_ARG(bool),
            QtCore.Q_ARG(str, title),
            QtCore.Q_ARG(str, message),
        )
        return bool(result)

    def run(self):
        # Publish parent config into the module-local threadlocal so
        # tools invoked from within this loop (notably `spawn_subagent`
        # and `view_hdf5_file`) can reuse the same LLM endpoint + reach
        # the parent's emit_tool + confirm + GUI-action helpers.
        # Cleared in `finally` so nested workers on the same thread
        # don't inherit stale values.
        from .subagents import WORKER_CTX
        WORKER_CTX.protocol   = self.protocol
        WORKER_CTX.base_url   = self.base_url
        WORKER_CTX.api_key    = self.api_key
        WORKER_CTX.model      = self.model
        WORKER_CTX.tool_ctx   = self.tool_ctx
        WORKER_CTX.emit_tool  = self._emit_tool
        WORKER_CTX.confirm    = self._confirm
        WORKER_CTX.gui_action = self._gui_action_helper
        try:
            if self.protocol == PROTOCOL_ANTHROPIC:
                text, usage = _chat_anthropic(
                    self.base_url, self.api_key, self.model,
                    self.system_prompt, self.history, self.user_text,
                    self._emit_tool, self._confirm, self.tool_ctx,
                )
            elif self.protocol == PROTOCOL_OPENAI:
                text, usage = _chat_openai(
                    self.base_url, self.api_key, self.model,
                    self.system_prompt, self.history, self.user_text,
                    self._emit_tool, self._confirm, self.tool_ctx,
                )
            else:
                self.error.emit(f"unknown protocol: {self.protocol!r}")
                return
            self.done.emit(text, usage)
        except ImportError as ex:
            self.error.emit(f"SDK not installed: {ex}")
        except Exception as ex:
            self.error.emit(f"{type(ex).__name__}: {ex}")
        finally:
            for attr in ("protocol", "base_url", "api_key", "model",
                         "tool_ctx", "emit_tool", "confirm", "gui_action"):
                if hasattr(WORKER_CTX, attr):
                    delattr(WORKER_CTX, attr)


# ── helpers for listing models ──────────────────────────────────────────

def _list_models(protocol, base_url, api_key, *, timeout=10.0):
    if protocol == PROTOCOL_ANTHROPIC:
        import anthropic
        client = anthropic.Anthropic(base_url=base_url, api_key=api_key,
                                     timeout=timeout, max_retries=0)
        return [m.id for m in client.models.list()]
    elif protocol == PROTOCOL_OPENAI:
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=_resolve_api_key(api_key),
                        timeout=timeout, max_retries=0)
        return [m.id for m in client.models.list().data]
    raise ValueError(f"unknown protocol: {protocol!r}")


# ── dialog ──────────────────────────────────────────────────────────────

# ── shared chat surface (used by both the popup and the bottom dock) ──

class AgentChatWidget(QtWidgets.QWidget):
    # Widget-level signals that outside listeners (e.g. AgentConsoleDialog)
    # can connect to. The internal `_ChatWorker` re-emits into these each
    # turn so consumers get a stable, per-widget wire regardless of which
    # per-turn worker is doing the actual work.
    tool_event   = pyqtSignal(str, dict, object)  # (name, args, result_or_None)
    user_sent    = pyqtSignal(str)                # (raw user text)
    assistant_replied = pyqtSignal(str, dict)     # (assistant text, usage dict)
    error_raised = pyqtSignal(str)                # (error message)
    """Just the chat surface — transcript, input, send / clear buttons,
    tool-toggle, status line. NO gateway / URL / key / model config here.

    Config comes from `load_settings()` at SEND time, not
    constructor. That means edits made in the full `AgentDialog` popup
    are picked up by the dock on the very next message — no signal
    plumbing between the two clients.

    If config is missing at send time, the widget shows a "not
    configured" line + a Configure… link that opens the full
    `AgentDialog` popup."""

    def __init__(self, parent=None, persist_id: Optional[str] = None):
        """`persist_id` — if given, the chat's transcript + history are
        saved to `~/.pystream/agent_history_<persist_id>.json` and
        restored on next construction. The dock passes `"dock"` so
        conversations survive pystream restarts; the popup passes
        nothing so it starts fresh each open."""
        super().__init__(parent)
        self._history: list[dict] = []
        # Concurrent turns — Send button stays enabled; each Send spawns
        # a new _ChatWorker onto its own QThread. We track live workers
        # in a set so they stay owned (Qt would otherwise garbage-
        # collect them mid-run) and so we can display an "N in flight"
        # count. Multiple workers append to `_history` on completion,
        # in COMPLETION order (not send order) — accepted trade-off for
        # not blocking the user on a long tool loop.
        self._workers: set = set()
        self._persist_id = persist_id
        # Lives on the GUI thread; workers route confirmation prompts
        # through it. Parenting to `self` keeps its lifetime tied to
        # the widget.
        self._confirm_helper = _ConfirmHelper(self)
        # GUI action bridge — lets tool functions ask the GUI thread
        # to open the embedded HDF5 viewer for a file path.
        self._gui_action_helper = _GuiActionHelper(self)
        self._build_ui()
        self._restore_history()

        # Publish this widget's state to the shared agents registry so
        # it shows up in the Agents panel. One publisher per widget
        # instance — the dock's Röntgen and the popup's Röntgen appear
        # as two rows if both are open. Long-lived; not a `with`.
        try:
            from .status import AgentStatusPublisher
            display_name = (load_settings().get("agent_name")
                            or DEFAULT_AGENT_NAME)
            suffix = f" ({persist_id})" if persist_id else ""
            self._status_pub = AgentStatusPublisher(
                name=f"{display_name}{suffix}",
                kind="main",
                ttl_s=120,   # generous — a long tool loop still counts as alive
                linger_s=0,  # widget is long-lived; don't linger on close
            )
            self._status_pub.idle("waiting for message")
            # Belt + suspenders on top of the module-level atexit hook:
            # Qt tears down widgets via aboutToQuit BEFORE the atexit
            # phase runs, so wire the cleanup there too. A clean-exit
            # publisher writes state=done and prevents the amber
            # "stale" cards from accumulating across restarts.
            qapp = QtWidgets.QApplication.instance()
            if qapp is not None:
                qapp.aboutToQuit.connect(self._on_app_quit)
        except Exception:
            self._status_pub = None

    # ── History persistence ─────────────────────────────────────────
    _HISTORY_MAX_TURNS = 100   # user+assistant pairs; caps unbounded growth

    def _history_path(self) -> Optional[str]:
        if not self._persist_id:
            return None
        return os.path.join(PYSTREAM_HOME,
                            f"agent_history_{self._persist_id}.json")

    def _restore_history(self):
        """Read saved (transcript, history) if persistence is enabled.
        Silent on missing / corrupt files — we start fresh in that case.
        After restore, scroll the transcript to the BOTTOM so the user
        sees the latest message on launch — the previous behaviour left
        the cursor at the top of a long history, which is disorienting."""
        path = self._history_path()
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self._history = list(data.get("history") or [])
            html = data.get("transcript_html") or ""
            if html:
                self.transcript.setHtml(html)
                self.status_label.setText(
                    f"restored {len(self._history)//2} turn(s) from previous session")
                self._scroll_transcript_to_bottom()
        except Exception:
            self._history = []

    def _scroll_transcript_to_bottom(self) -> None:
        """Force the transcript view to the last message. Called after
        restore + after every append. Uses `singleShot(0, …)` so the
        scroll happens AFTER Qt lays out the freshly-set HTML (setting
        HTML doesn't itself trigger a layout pass synchronously — a
        direct scroll here would land at wherever the layout WAS)."""
        def _pin():
            sb = self.transcript.verticalScrollBar()
            sb.setValue(sb.maximum())
            # Also nudge the text cursor to the end so keyboard nav
            # (Ctrl+End is redundant, but visual cursor position sits
            # at the last character rather than the top).
            cur = self.transcript.textCursor()
            cur.movePosition(QtGui.QTextCursor.End)
            self.transcript.setTextCursor(cur)
        QtCore.QTimer.singleShot(0, _pin)

    def _save_history(self):
        """Persist current transcript + history. Called after every
        successful assistant reply. Cap to _HISTORY_MAX_TURNS so files
        don't grow unbounded."""
        path = self._history_path()
        if not path:
            return
        try:
            os.makedirs(PYSTREAM_HOME, exist_ok=True)
            # Trim oldest turns; each turn is 2 messages (user + assistant)
            keep = self._HISTORY_MAX_TURNS * 2
            trimmed = self._history[-keep:] if len(self._history) > keep else self._history
            with open(path, "w") as f:
                json.dump({
                    "history":         trimmed,
                    "transcript_html": self.transcript.toHtml(),
                }, f, indent=2)
        except Exception:
            pass

    def _build_ui(self):
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        # Chat transcript
        self.transcript = QtWidgets.QTextBrowser()
        self.transcript.setOpenExternalLinks(True)
        self.transcript.setStyleSheet(
            "QTextBrowser { background-color: #1e1e1e; color: #e0e0e0; "
            "font-family: 'DejaVu Sans Mono', monospace; font-size: 10pt; }"
        )
        lay.addWidget(self.transcript, stretch=1)

        # Input row
        irow = QtWidgets.QHBoxLayout()
        self.input_edit = QtWidgets.QLineEdit()
        self.input_edit.setPlaceholderText("Type a question and press Enter…")
        self.input_edit.returnPressed.connect(self._on_send)
        irow.addWidget(self.input_edit)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send)
        irow.addWidget(self.send_btn)
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.setToolTip("Clear conversation history")
        self.clear_btn.clicked.connect(self._on_clear)
        irow.addWidget(self.clear_btn)
        # Settings button — opens the full AgentDialog popup where the
        # gateway URL / API key / model / system prompt live. Reuses
        # pystream's singleton instance if one is already open, so both
        # entry points (this button + the toolbar "AI") share one dialog.
        self.settings_btn = QtWidgets.QPushButton("⚙ Settings")
        self.settings_btn.setToolTip(
            "Open the full AI Agent dialog to configure the gateway "
            "URL, API key, model, and system prompt.")
        self.settings_btn.clicked.connect(self._on_settings_click)
        irow.addWidget(self.settings_btn)
        lay.addLayout(irow)

        # Tool-verbosity toggle
        self.show_tools_chk = QtWidgets.QCheckBox("Show tool calls")
        self.show_tools_chk.setChecked(False)
        self.show_tools_chk.setToolTip(
            "Toggle whether each tool the agent calls is shown in the "
            "transcript. Off = clean conversation. On = full audit trail.")
        lay.addWidget(self.show_tools_chk)

        # Status line
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #888; font-size: 9pt;")
        lay.addWidget(self.status_label)

    # ── chat flow ────────────────────────────────────────────────────
    def _load_config(self) -> Optional[dict]:
        """Read persisted config + beamline tool context. Returns None if
        URL/key/model missing. Called at SEND time so config edits (in
        AgentDialog) and beamline changes are picked up on the very
        next message."""
        s = load_settings() or {}
        url = (s.get("base_url") or "").strip()
        key = (s.get("api_key") or "").strip()
        model = (s.get("model") or "").strip()
        # `key` is optional now — local OpenAI-compatible runners
        # (Ollama, llama.cpp, vLLM, LM Studio) don't need one, and
        # `_chat_openai` substitutes a sentinel when it's blank.
        # `url` and `model` are still required — without them we
        # have no server to talk to and no model to select.
        if not (url and model):
            return None
        tool_ctx = _load_tool_context()
        name = (s.get("agent_name") or DEFAULT_AGENT_NAME).strip() or DEFAULT_AGENT_NAME
        addendum = tool_ctx.get("system_prompt_addendum", "") or ""
        prompt = (
            (s.get("system_prompt") or SYSTEM_PROMPT_DEFAULT)
            .replace("{name}",              name)
            .replace("{beamline}",          _active_beamline())
            .replace("{beamline_addendum}", addendum)
        )
        return {
            "protocol":      s.get("protocol", PROTOCOL_ANTHROPIC),
            "url":           url,
            "key":           key,
            "model":         model,
            "system_prompt": prompt,
            "agent_name":    name,
            "beamline":      _active_beamline(),
            "tool_ctx":      tool_ctx,
        }

    def _on_send(self):
        text = self.input_edit.text().strip()
        if not text:
            return
        cfg = self._load_config()
        if cfg is None:
            self._append_transcript("error",
                "Not configured. Open the AI Agent popup "
                "(Tools ▾ → AI) and set Gateway URL, API key, and model.")
            self.status_label.setText("not configured")
            return

        self._append_transcript("user", text)
        self.user_sent.emit(text)
        self.input_edit.clear()
        # Send stays enabled — user can queue more messages while this
        # one is running. Each Send spawns its own worker on its own
        # QThread; they run in parallel.

        worker = _ChatWorker(
            protocol=cfg["protocol"],
            base_url=cfg["url"], api_key=cfg["key"], model=cfg["model"],
            system_prompt=cfg["system_prompt"],
            # Snapshot history AT SEND TIME. If a previous turn is
            # still running, its future response isn't in this
            # snapshot — accepted trade-off for allowing parallelism.
            history=list(self._history),
            user_text=text,
            tool_ctx=cfg["tool_ctx"],
            confirm_helper=self._confirm_helper,
            gui_action_helper=self._gui_action_helper,
        )
        # Bind the user_text into the completion callback so multiple
        # concurrent workers each append their OWN user message to
        # history (not whatever _pending_user_text happens to be).
        worker.tool_event.connect(self._on_tool_event)
        worker.done.connect(
            lambda a_text, usage, u=text: self._on_worker_done(u, a_text, usage))
        worker.error.connect(self._on_worker_error)
        worker.finished.connect(
            lambda w=worker: self._on_worker_finished(w))
        self._workers.add(worker)
        worker.start()

        self.status_label.setText(
            f"…thinking ({len(self._workers)} in flight)")

        # Publish "thinking" so the Agents panel shows this widget as
        # active. Every tool call bumps the activity line further
        # inside _on_tool_event.
        if self._status_pub is not None:
            n = len(self._workers)
            suffix = f" [{n} in flight]" if n > 1 else ""
            self._status_pub.activity(
                f"thinking: {text[:60]}" + ("…" if len(text) > 60 else "") + suffix)

    def _on_tool_event(self, name, args, result):
        # Publish to the Agents panel regardless of the transcript
        # verbosity toggle — the panel wants to know what tool is
        # running even when the chat is set to compact mode.
        if self._status_pub is not None:
            phase = "calling" if result is None else "got result from"
            self._status_pub.activity(f"{phase} tool: {name}")
        # Forward to any external listeners (Agent Console window etc.).
        # Unconditional — the console is a separate surface and does its
        # own filtering; the transcript's show_tools checkbox is only
        # about the in-chat display.
        try:
            self.tool_event.emit(name, dict(args) if args else {}, result)
        except Exception:
            pass
        if not self.show_tools_chk.isChecked():
            return
        if result is None:
            self._append_tool_call(name, args)
        else:
            self._append_tool_result(name, result)

    def _on_worker_done(self, user_text, assistant_text, usage):
        # `user_text` is bound at Send time via lambda closure so the
        # right user turn is paired with the right assistant turn even
        # with multiple workers running in parallel and finishing out
        # of order.
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": assistant_text})
        self._append_transcript("assistant", assistant_text)
        try:
            self.assistant_replied.emit(assistant_text, dict(usage) if usage else {})
        except Exception:
            pass
        # Persist so a pystream restart resumes this conversation.
        self._save_history()
        cr = usage.get("cache_read", 0)
        cw = usage.get("cache_write", 0)
        cache_str = ""
        if cr:
            cache_str = f", cache hit {cr}"
        elif cw:
            cache_str = f", cache write {cw}"
        self.status_label.setText(
            f"in {usage.get('input', 0)}{cache_str}, "
            f"out {usage.get('output', 0)} tokens"
        )

    def _on_worker_error(self, msg):
        self._append_transcript("error", msg)
        self.status_label.setText("error")
        try:
            self.error_raised.emit(msg)
        except Exception:
            pass
        if self._status_pub is not None:
            self._status_pub.error(msg[:120])

    def _on_worker_finished(self, worker=None):
        # Discard finished worker so Qt can GC it. Update the status
        # label with the remaining in-flight count.
        if worker is not None:
            self._workers.discard(worker)
        n = len(self._workers)
        if n:
            self.status_label.setText(f"…thinking ({n} in flight)")
            if self._status_pub is not None:
                self._status_pub.activity(f"{n} turn(s) in flight")
        else:
            if self._status_pub is not None:
                self._status_pub.idle("waiting for message")

    def _on_app_quit(self):
        """Called from QApplication.aboutToQuit — mark the record done
        so the next launch doesn't see a stale amber card."""
        pub = getattr(self, "_status_pub", None)
        if pub is not None and not pub._closed:
            try:
                pub.finish("pystream closed")
            except Exception:
                pass

    def _on_clear(self):
        self._history = []
        self.transcript.clear()
        self.status_label.setText("history cleared")
        # Wipe the persisted file too, so the next launch starts fresh.
        path = self._history_path()
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass

    # ── transcript rendering ─────────────────────────────────────────
    def _append_transcript(self, role, text):
        text_html = self._escape(text).replace("\n", "<br>")
        if role == "user":
            html = f"<p><b style='color:#7fbf7f;'>You:</b> {text_html}</p>"
        elif role == "assistant":
            name = self._escape(self._agent_name())
            html = f"<p><b style='color:#88aaee;'>{name}:</b> {text_html}</p>"
        else:
            html = f"<p><b style='color:#e07070;'>Error:</b> {text_html}</p>"
        self.transcript.append(html)

    def _agent_name(self) -> str:
        """Read the current display name from settings. Empty → default."""
        s = load_settings() or {}
        n = (s.get("agent_name") or "").strip()
        return n or DEFAULT_AGENT_NAME

    def _append_tool_call(self, name, args):
        try:
            args_str = json.dumps(args, default=str)
        except Exception:
            args_str = repr(args)
        self.transcript.append(
            f"<p style='color:#aaa; margin-left: 12px;'>"
            f"⏵ <b>{self._escape(name)}</b>({self._escape(args_str)})</p>"
        )

    def _append_tool_result(self, name, result):
        display = result
        if isinstance(result, dict) and result.get("image_base64"):
            display = {k: v for k, v in result.items() if k != "image_base64"}
            display["_image"] = (
                f"<{display.get('media_type', 'image')}, "
                f"{display.get('png_kb', '?')} KB, embedded for vision>"
            )
        try:
            res_str = json.dumps(display, default=str)
        except Exception:
            res_str = repr(display)
        if len(res_str) > 600:
            res_str = res_str[:600] + " …"
        is_error = isinstance(result, dict) and "error" in result
        color = "#e07070" if is_error else "#777"
        self.transcript.append(
            f"<p style='color:{color}; margin-left: 24px;'>"
            f"  ↳ {self._escape(res_str)}</p>"
        )

    @staticmethod
    def _escape(text):
        return (str(text).replace("&", "&amp;")
                          .replace("<", "&lt;")
                          .replace(">", "&gt;"))

    def wait_for_worker(self, timeout_ms: int = 2000):
        """For clean shutdown from the enclosing dialog / dock."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(timeout_ms)

    def _on_settings_click(self):
        """Open the full AgentDialog (settings + chat popup). Uses
        pystream's singleton instance if one exists (created by the
        toolbar "AI" button), so both entry points share one dialog
        and one persistent config state."""
        # If this widget IS already inside an AgentDialog (i.e. the
        # popup — not the bottom dock), just make sure the dialog is
        # visible/focused instead of opening a second copy.
        top = self.window()
        if isinstance(top, AgentDialog):
            top.raise_()
            top.activateWindow()
            return

        # Find the pystream main window (typically our top-level).
        main_window = top
        attr = 'agentdialog_instance'
        inst = getattr(main_window, attr, None)
        if inst is None or not isinstance(inst, AgentDialog):
            inst = AgentDialog(parent=main_window)
            try:
                setattr(main_window, attr, inst)
            except Exception:
                pass
        inst.show()
        inst.raise_()
        inst.activateWindow()


# ── build the bottom-dock wrapper — used by pystream's main window ────

def build_agent_panel(parent_window, persist_id: str = "dock") -> QtWidgets.QWidget:
    """Return an AgentChatWidget suitable for insertion into pystream's
    central vertical splitter as a bottom panel.

    `persist_id` — history file suffix. Default "dock" saves to
    ~/.pystream/agent_history_dock.json. Pass a different id if a caller
    needs a distinct conversation.

    No QDockWidget — the widget is a regular child of the splitter, so
    the user gets a resize handle above it and can drag to change its
    height. No floating, no title-bar drag, no accidental undocking.
    Hide/show via the View menu."""
    w = AgentChatWidget(parent_window, persist_id=persist_id)
    # Lowish minimum so the panel can shrink to a compact strip,
    # but not so low that content disappears entirely.
    w.setMinimumHeight(80)
    # Preferred size policy on both axes so the splitter can hand it
    # more or less space as the user drags the handle. Without an
    # explicit Expanding vertical policy, some Qt styles let the
    # widget refuse to grow past its sizeHint.
    w.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                    QtWidgets.QSizePolicy.Expanding)
    return w


# ── full popup dialog: settings above + shared chat widget below ──────

class AgentDialog(QtWidgets.QDialog):
    """Chat + gateway/URL/key/model settings. Singleton — open once,
    settings persist across sessions. The chat surface is the same
    `AgentChatWidget` embedded in the bottom dock."""

    BUTTON_TEXT = "AI"
    GROUP       = "Tools"
    HANDLER_TYPE = "singleton"

    def __init__(self, parent=None, logger: Optional[logging.Logger] = None):
        super().__init__(parent)
        self.logger = logger
        self.setWindowTitle("AI Agent")
        self.resize(720, 640)

        self._build_ui()
        self._restore_settings()
        # Knowledge-base bootstrap is beamline-specific — done by
        # bl32ID.start_background_services on pystream launch, not here.

    # ── UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        lay = QtWidgets.QVBoxLayout(self)

        setup = QtWidgets.QGroupBox("Gateway")
        sl = QtWidgets.QFormLayout()

        # Backend preset — cloud vs. locally-hosted. The wire protocol
        # is derived from the backend (local rides PROTOCOL_OPENAI
        # because every popular local runner exposes an OpenAI-compat
        # endpoint). Power users can still override the raw Protocol
        # below.
        self.backend_combo = QtWidgets.QComboBox()
        for bid, label, _proto, _placeholder, _needs_key in BACKEND_PRESETS:
            self.backend_combo.addItem(label, bid)
        self.backend_combo.setToolTip(
            "Cloud API or a locally-hosted model. Local — OpenAI-"
            "compatible works with Ollama, llama.cpp, vLLM, LM Studio "
            "and other runners that expose a /v1 endpoint; the API-"
            "key field can be left blank for those.")
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        sl.addRow("Backend:", self.backend_combo)

        self.protocol_combo = QtWidgets.QComboBox()
        self.protocol_combo.addItem("Anthropic Messages API", PROTOCOL_ANTHROPIC)
        self.protocol_combo.addItem("OpenAI Chat Completions", PROTOCOL_OPENAI)
        self.protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        sl.addRow("Protocol:", self.protocol_combo)

        # Agent's display name — shown as the transcript prefix and
        # substituted into every `{name}` placeholder in the system
        # prompt. Default is DEFAULT_AGENT_NAME (Röntgen). Free-form.
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText(DEFAULT_AGENT_NAME)
        self.name_edit.setToolTip(
            f"Agent's display name. Default '{DEFAULT_AGENT_NAME}'. "
            "Shows up as the transcript prefix and replaces every "
            "{name} placeholder in the system prompt.")
        sl.addRow("Agent name:", self.name_edit)

        self.url_edit = QtWidgets.QLineEdit()
        sl.addRow("Base URL:", self.url_edit)

        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setToolTip(
            "Saved locally in ~/.pystream_bl32ID_settings.json "
            "(user-only file; not in any git repo)."
        )
        sl.addRow("API key:", self.key_edit)

        row = QtWidgets.QHBoxLayout()
        self.connect_btn = QtWidgets.QPushButton("Connect / refresh models")
        self.connect_btn.clicked.connect(self._refresh_models)
        row.addWidget(self.connect_btn)
        self.conn_status = QtWidgets.QLabel("not connected")
        self.conn_status.setStyleSheet("color: #888;")
        row.addWidget(self.conn_status)
        row.addStretch()
        rw = QtWidgets.QWidget()
        rw.setLayout(row)
        sl.addRow("", rw)

        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setEditable(True)
        sl.addRow("Model:", self.model_combo)
        setup.setLayout(sl)
        lay.addWidget(setup)

        # System prompt (collapsible)
        sys_box = QtWidgets.QGroupBox("System prompt (click to expand)")
        sys_box.setCheckable(True)
        sys_box.setChecked(False)
        svl = QtWidgets.QVBoxLayout()
        self.system_edit = QtWidgets.QTextEdit()
        self.system_edit.setPlainText(SYSTEM_PROMPT_DEFAULT)
        self.system_edit.setMaximumHeight(160)
        svl.addWidget(self.system_edit)
        reset_row = QtWidgets.QHBoxLayout()
        reset_row.addStretch()
        self.reset_prompt_btn = QtWidgets.QPushButton("Reset to default")
        self.reset_prompt_btn.setToolTip(
            "Replace the saved system prompt with the current built-in "
            "default. Use this after a code update changes the default "
            "guidance.")
        self.reset_prompt_btn.clicked.connect(
            lambda: self.system_edit.setPlainText(SYSTEM_PROMPT_DEFAULT))
        reset_row.addWidget(self.reset_prompt_btn)
        svl.addLayout(reset_row)
        sys_box.setLayout(svl)
        self.system_edit.setVisible(False)
        self.reset_prompt_btn.setVisible(False)
        sys_box.toggled.connect(self.system_edit.setVisible)
        sys_box.toggled.connect(self.reset_prompt_btn.setVisible)
        lay.addWidget(sys_box)

        # Shared chat surface — same widget class the bottom dock uses.
        # Persist config edits so the dock (which re-reads settings at
        # send-time) picks up any changes made here.
        self.chat = AgentChatWidget(self)
        lay.addWidget(self.chat, stretch=1)

        # Initial placeholder / label setup — always driven by the
        # Backend combobox (which itself defaults to Anthropic (cloud)
        # until _restore_settings picks the saved backend).
        self._on_backend_changed()

    def _on_protocol_changed(self):
        # Called when the user tweaks the Protocol combobox directly
        # (independent of the Backend preset). Only refresh the URL
        # placeholder to match the raw protocol; leave the Backend
        # combobox alone so the user's explicit choice isn't reset.
        proto = self._current_protocol()
        if proto == PROTOCOL_ANTHROPIC:
            self.url_edit.setPlaceholderText(
                "https://gateway.example.com (Anthropic Messages — no /v1)"
            )
        else:
            self.url_edit.setPlaceholderText(
                "https://gateway.example.com/v1 (OpenAI)"
            )

    def _current_backend(self) -> str:
        """Backend id currently selected in the Backend combobox."""
        return self.backend_combo.currentData() or BACKEND_ANTHROPIC

    def _on_backend_changed(self):
        """Update Protocol + URL placeholder + API-key hint from the
        selected preset. Does NOT clear whatever the user has already
        typed — placeholders are cosmetic; existing text stays."""
        bid = self._current_backend()
        preset = next((p for p in BACKEND_PRESETS if p[0] == bid), None)
        if preset is None:
            return
        _, _label, proto, placeholder, needs_key = preset

        # Sync the Protocol combo to the backend's protocol without
        # letting _on_protocol_changed re-clobber the URL placeholder
        # we're about to set.
        idx = self.protocol_combo.findData(proto)
        if idx >= 0 and self.protocol_combo.currentIndex() != idx:
            self.protocol_combo.blockSignals(True)
            self.protocol_combo.setCurrentIndex(idx)
            self.protocol_combo.blockSignals(False)

        self.url_edit.setPlaceholderText(placeholder)

        if needs_key:
            self.key_edit.setPlaceholderText("required for cloud gateway")
            self.key_edit.setToolTip(
                "Cloud API key. Saved locally in "
                "~/.pystream/agent_settings.json (user-only file; not "
                "in any git repo).")
        else:
            self.key_edit.setPlaceholderText(
                "leave blank for local runners; a sentinel is sent "
                "automatically")
            self.key_edit.setToolTip(
                "Local runners (Ollama, llama.cpp, vLLM, LM Studio) "
                "ignore the API key. If your runner does require one, "
                "enter it here — otherwise leave blank.")

    def _current_protocol(self) -> str:
        return self.protocol_combo.currentData() or PROTOCOL_ANTHROPIC

    # ── connection / models ─────────────────────────────────────────────
    def _refresh_models(self):
        url = self.url_edit.text().strip()
        key = self.key_edit.text().strip()
        if not url:
            self._set_conn_status("set URL first", error=True)
            return
        # Key is optional for local backends (`_list_models` will
        # substitute a sentinel via `_resolve_api_key`). For the two
        # cloud backends, warn but don't block — the server itself
        # will 401 and the user will see the error inline.
        proto = self._current_protocol()
        self._set_conn_status("connecting…", error=False)
        QtWidgets.QApplication.processEvents()
        try:
            ids = _list_models(proto, url, key)
        except ImportError as ex:
            self._set_conn_status(f"SDK missing: {ex}", error=True)
            return
        except Exception as ex:
            self._set_conn_status(f"{type(ex).__name__}: {ex}", error=True)
            return
        if not ids:
            self._set_conn_status("connected, but no models exposed", error=True)
            return
        prev = self.model_combo.currentText().strip()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for mid in sorted(ids):
            self.model_combo.addItem(mid)
        if prev and prev in ids:
            self.model_combo.setCurrentText(prev)
        self.model_combo.blockSignals(False)
        self._set_conn_status(f"connected — {len(ids)} models", error=False)
        self._persist_settings()

    def _set_conn_status(self, text, *, error):
        self.conn_status.setText(text)
        self.conn_status.setStyleSheet(
            "color: #c66;" if error else "color: #6a6;"
        )

    # ── settings ────────────────────────────────────────────────────────
    def _restore_settings(self):
        s = load_settings()
        if not s:
            return
        # Backend first — it drives placeholders + key hint via
        # _on_backend_changed. Silence the signal while we set it so
        # user text isn't overwritten by placeholder logic before
        # url_edit / key_edit get populated below.
        backend = _backend_from_settings(s)
        bidx = self.backend_combo.findData(backend)
        if bidx >= 0:
            self.backend_combo.blockSignals(True)
            self.backend_combo.setCurrentIndex(bidx)
            self.backend_combo.blockSignals(False)
        proto = s.get("protocol", PROTOCOL_ANTHROPIC)
        idx = self.protocol_combo.findData(proto)
        if idx >= 0:
            self.protocol_combo.setCurrentIndex(idx)
        self.name_edit.setText(s.get("agent_name", ""))
        self.url_edit.setText(s.get("base_url", ""))
        self.key_edit.setText(s.get("api_key", ""))
        # Now that url/key are populated, sync the cosmetic bits
        # (placeholders + key hint) to the restored backend.
        self._on_backend_changed()
        sp = s.get("system_prompt")
        if sp:
            self.system_edit.setPlainText(sp)
        last_model = s.get("model")
        if last_model:
            self.model_combo.addItem(last_model)
            self.model_combo.setCurrentText(last_model)
        self.chat.show_tools_chk.setChecked(bool(s.get("show_tool_calls", False)))

    def _persist_settings(self):
        save_settings({
            "backend": self._current_backend(),
            "protocol": self._current_protocol(),
            "agent_name": self.name_edit.text().strip(),
            "base_url": self.url_edit.text(),
            "api_key": self.key_edit.text(),
            "system_prompt": self.system_edit.toPlainText(),
            "model": self.model_combo.currentText(),
            "show_tool_calls": self.chat.show_tools_chk.isChecked(),
        })

    # ── knowledge-base bootstrap ────────────────────────────────────────

    def closeEvent(self, event):
        self._persist_settings()
        # Chat's worker is owned by AgentChatWidget now.
        self.chat.wait_for_worker(2000)
        super().closeEvent(event)
