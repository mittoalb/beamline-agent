"""Sub-agent dispatch — one chat, many specialists.

Röntgen (the main pystream chat) is the ORCHESTRATOR. When a user's ask
maps onto a specialized workflow (tomographic reconstruction, sample
alignment, XANES setup, …), Röntgen delegates via the
`spawn_subagent(kind, task)` tool defined here rather than trying to do
the work itself. This keeps the user in ONE chat surface — the sub-
agent is a background worker whose progress shows up in the Agents
panel and whose tool calls stream into the Console, but whose final
answer comes back through Röntgen's transcript.

Preset per kind: display name for the Agents panel, a `.md` doc that
becomes the sub-agent's system prompt, a subset of tool names the
sub-agent is allowed to use, and a purpose sentence. Add a kind → add
an entry to `SUBAGENT_KINDS`. Everything else is generic.

Config source: the sub-agent reuses the parent chat's runtime config
(protocol, url, api key, model) via the module-level threadlocal
`WORKER_CTX` — the parent `_ChatWorker` sets these on entry to `run()`
and clears them on exit, so a tool called from that worker can pick
them up. Fresh settings from `~/.pystream/agent_settings.json` are the
fallback if a tool is somehow invoked outside a worker.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

# Runtime-config bridge from the parent _ChatWorker into any tool it
# calls. The worker fills these before executing tool calls; the
# `spawn_subagent` tool reads them to reuse the same LLM endpoint.
WORKER_CTX = threading.local()


def _worker_field(name: str, fallback=None):
    return getattr(WORKER_CTX, name, fallback)


# ── preset registry ──────────────────────────────────────────────────

def _read_doc(path: str) -> str:
    """Read a system-prompt .md file. Returns a placeholder string if
    missing so the sub-agent still runs but reports the gap."""
    p = os.path.expanduser(path)
    try:
        with open(p) as f:
            return f.read()
    except OSError as e:
        return (f"(tomogui.md missing at {p} — running with degraded "
                f"knowledge. Error: {e})")


# Kind → { display_name, purpose, doc_path, tool_names }.
# Extend to add specialists (sample_alignment, xanes_setup, …).
SUBAGENT_KINDS: dict[str, dict] = {
    "reconstruction": {
        "display_name": "Reconstruction subagent",
        "purpose":      ("drive tomogui-cli headlessly for tomographic "
                         "reconstruction jobs on the beamline's GPU nodes"),
        "doc_path":     "~/.pystream/docs/tomogui.md",
        # Tool subset — the sub-agent gets ONLY these from the
        # parent's tool_ctx. Keeps its budget focused.
        "tool_names":   ["bash", "save_learned_note"],
        # Per-turn iteration cap for the sub-agent's own tool loop.
        # Reconstructions need only a couple rounds if instructions
        # are followed; cap prevents runaway spawn.
        "max_iterations": 8,
    },
    "physicist": {
        "display_name": "Physicist subagent",
        "purpose":      ("answer physics questions related to x-ray "
                         "optics, tomography, matter interactions, "
                         "diffraction, coherence, detector physics"),
        "doc_path":     "~/.pystream/docs/physicist.md",
        "tool_names":   ["bash", "fetch_url", "read_file",
                         "save_learned_note"],
        "max_iterations": 6,
    },
    "chemist": {
        "display_name": "Chemist subagent",
        "purpose":      ("answer chemistry questions related to XANES / "
                         "EXAFS interpretation, absorption edges, "
                         "chemical composition inference"),
        "doc_path":     "~/.pystream/docs/chemist.md",
        "tool_names":   ["bash", "fetch_url", "read_file",
                         "save_learned_note"],
        "max_iterations": 6,
    },
    "beamline_operator": {
        "display_name": "Beamline operator subagent",
        "purpose":      ("perform multi-step beamline work — open "
                         "plugins in sequence, run alignments, coordinate "
                         "operational tasks that would burn Röntgen's "
                         "tool rounds if done inline"),
        "doc_path":     "~/.pystream/docs/beamline_operator.md",
        # Broad tool set — this is the operational specialist. Note
        # PV / plugin tools are only present if the active beamline
        # contributes them (bl32ID does; bl19BM currently doesn't).
        "tool_names":   [
            "bash", "read_file",
            "read_pv", "caput",
            "open_beamline_plugin", "list_beamline_plugins",
            "view_hdf5_file",
            "view_detector_image", "get_detector_image_stats",
            "list_status_pages", "fetch_url",
            "list_task_recordings", "read_task_recording",
            "save_learned_note",
        ],
        "max_iterations": 10,
    },
}


# ── the tool ─────────────────────────────────────────────────────────

_SUBAGENT_PROMPT_TEMPLATE = """You are a **specialized sub-agent** spawned by
Röntgen (the pystream orchestrator). You were dispatched because your
prompt + tool set are the right match for the user's task.

**Task from your parent (verbatim):**
    {task}

**Rules for your reply:**
- Do the task. Return a SHORT summary of what you did and the outcome
  (≤ 200 words unless the task explicitly asks for detail). The parent
  will present this back to the user.
- Do NOT ask clarifying questions — the parent already gathered the
  info. If something is ambiguous, make a reasonable call and note it
  in the summary; the parent will loop back if needed.
- Do NOT greet, chit-chat, or restate the task — output is pure
  work-product.
- If you can't do the task, say so concisely + reason. Do not loop
  trying to work around it.

────────────────────  YOUR PURPOSE  ────────────────────

{purpose}

────────────────────  INSTRUCTIONS  ────────────────────

{doc}
"""


def tool_spawn_subagent(kind: str, task: str) -> dict:
    """Delegate a specialized task to a purpose-built sub-agent.

    The sub-agent runs SYNCHRONOUSLY on the parent's worker thread with
    its own system prompt + narrow tool set + fresh conversation
    context (no shared history). Its final message is returned to
    Röntgen as this tool's result. Its progress shows in the Agents
    panel; its tool calls stream into the Console alongside Röntgen's."""
    preset = SUBAGENT_KINDS.get(kind)
    if not preset:
        return {"error": f"unknown subagent kind {kind!r}",
                "available_kinds": sorted(SUBAGENT_KINDS)}
    if not task or not task.strip():
        return {"error": "task is required"}

    # ── pull parent config from the worker threadlocal ──
    protocol = _worker_field("protocol")
    base_url = _worker_field("base_url")
    api_key  = _worker_field("api_key")
    model    = _worker_field("model")
    parent_tool_ctx = _worker_field("tool_ctx") or {}
    emit_tool = _worker_field("emit_tool")
    confirm   = _worker_field("confirm")
    if not (protocol and base_url and api_key and model):
        # Fallback for the (unlikely) case that this tool was invoked
        # outside a chat worker — pull from settings.
        try:
            from .chat import load_settings, PROTOCOL_ANTHROPIC
            cfg = load_settings()
            protocol = cfg.get("protocol", PROTOCOL_ANTHROPIC)
            base_url = cfg.get("base_url")
            api_key  = cfg.get("api_key")
            model    = cfg.get("model")
        except Exception:
            return {"error": "no runtime config available (called outside worker + settings unreadable)"}
    if not (base_url and api_key and model):
        return {"error": "parent chat has no LLM config — set it in AI Settings first"}

    # ── build the sub-agent's system prompt ──
    doc_text = _read_doc(preset["doc_path"])
    system_prompt = _SUBAGENT_PROMPT_TEMPLATE.format(
        task=task.strip(),
        purpose=preset["purpose"],
        doc=doc_text,
    )

    # ── build the sub-agent's tool subset from the parent's catalog ──
    allowed = set(preset.get("tool_names") or [])
    sub_specs_anthropic = [
        t for t in parent_tool_ctx.get("tool_specs_anthropic", [])
        if t.get("name") in allowed
    ]
    sub_specs_openai = [
        t for t in parent_tool_ctx.get("tool_specs_openai", [])
        if (t.get("function") or {}).get("name") in allowed
    ]
    parent_get_tool = parent_tool_ctx.get("get_tool") or (lambda _n: None)
    def _sub_get_tool(name: str):
        if name not in allowed:
            return None
        return parent_get_tool(name)
    sub_tool_ctx = dict(parent_tool_ctx)
    sub_tool_ctx["tool_specs_anthropic"] = sub_specs_anthropic
    sub_tool_ctx["tool_specs_openai"] = sub_specs_openai
    sub_tool_ctx["get_tool"] = _sub_get_tool
    # Sub-agent's own prompt already has the doc content baked in — its
    # merged addendum shouldn't add anything more or the prompt bloats.
    sub_tool_ctx["system_prompt_addendum"] = ""

    # ── publish to the Agents panel + parent-link via env var ──
    from .status import AgentStatusPublisher
    parent_id = os.environ.get("APS_AGENT_PARENT_ID")
    started = time.time()

    with AgentStatusPublisher(
        name=preset["display_name"],
        kind="subagent",
        parent=parent_id,
        ttl_s=60,
    ) as pub:
        pub.activity(f"running: {task[:80]}" + ("…" if len(task) > 80 else ""))

        # ── run the sub-agent's own chat loop ──
        try:
            from .chat import (
                _chat_anthropic, _chat_openai,
                PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI,
                MAX_AGENT_ITERATIONS,
            )
            # Temporarily raise the iteration cap for the sub-agent if
            # its preset specifies one different from the default.
            # `_chat_*` reads MAX_AGENT_ITERATIONS at call time, so
            # patch it in the module namespace for the duration of
            # this call. Restored in finally.
            import pystream.agent.chat as chat_mod
            orig_cap = chat_mod.MAX_AGENT_ITERATIONS
            chat_mod.MAX_AGENT_ITERATIONS = int(preset.get("max_iterations")
                                                or orig_cap)
            try:
                if protocol == PROTOCOL_ANTHROPIC:
                    result_text, usage = _chat_anthropic(
                        base_url, api_key, model,
                        system_prompt=system_prompt,
                        history=[],
                        user_text=task,
                        emit_tool=emit_tool or (lambda *a, **kw: None),
                        confirm=confirm,
                        tool_ctx=sub_tool_ctx,
                    )
                elif protocol == PROTOCOL_OPENAI:
                    result_text, usage = _chat_openai(
                        base_url, api_key, model,
                        system_prompt=system_prompt,
                        history=[],
                        user_text=task,
                        emit_tool=emit_tool or (lambda *a, **kw: None),
                        confirm=confirm,
                        tool_ctx=sub_tool_ctx,
                    )
                else:
                    pub.error(f"unknown protocol {protocol!r}")
                    return {"error": f"unknown protocol {protocol!r}"}
            finally:
                chat_mod.MAX_AGENT_ITERATIONS = orig_cap
        except Exception as ex:
            pub.error(f"{type(ex).__name__}: {ex}")
            return {"error": f"sub-agent crashed: {type(ex).__name__}: {ex}",
                    "kind": kind}

        elapsed = time.time() - started
        pub.finish(f"done in {elapsed:0.1f}s")

    return {
        "kind": kind,
        "display_name": preset["display_name"],
        "result": result_text,
        "elapsed_s": round(elapsed, 2),
        "usage": usage,
    }
