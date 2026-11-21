"""Read-only Slack bot for beamline status.

Ask from Slack what the beamline is doing — is a scan running, which
one, how far along, where the data is going, is there beam, is the
camera acquiring — and get an answer from the same agent (same model
settings and beamline prompt as pystream's AI Agent dock), restricted
to READ-ONLY tools. It cannot move motors, write PVs, start/stop
scans or IOCs, run shell commands or read arbitrary files.

Run (on any beamline host; pystream doesn't need to be open):

    python -m beamline_agent.slack_bridge

Slack setup (once): create an app at api.slack.com/apps →
  * Socket Mode: ON → app-level token with `connections:write` (xapp-…)
  * OAuth scopes (bot): app_mentions:read, chat:write, im:history,
    im:read, im:write, files:write, channels:history (+ groups:history
    for private channels)
  * Event subscriptions (bot events): app_mention, message.im
  * Install to workspace → bot token (xoxb-…); /invite the bot to a channel

Config: ~/.pystream/slack_bridge.json (chmod 600 — it holds tokens)

    {
      "bot_token": "xoxb-…",          # or env SLACK_BOT_TOKEN
      "app_token": "xapp-…",          # or env SLACK_APP_TOKEN
      "allowed_users":    ["U012ABC"],   # Slack member IDs; [] = anyone
      "allowed_channels": ["C034DEF"]    # channel IDs; [] = any (DMs ok)
    }

Usage in Slack: DM the bot, or @mention it in a channel.
  `status`           → one-shot snapshot (no LLM call, ~2 s)
  anything else      → read-only agent answer, in a thread
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

LOGGER = logging.getLogger("beamline_agent.slack_bridge")

CONFIG_FILE = os.path.expanduser("~/.pystream/slack_bridge.json")

# The ONLY tools the Slack agent can see or run. Anything else is
# hidden from the model and refused if called. Deliberately absent:
# bash (can run anything), read_file (could post API keys/tokens),
# caput, ioc_action, tomoscan_start/abort, GUI/viewer tools,
# spawn_subagent, save_learned_note.
READ_ONLY_TOOLS = frozenset({
    # status
    "beamline_snapshot", "list_running_scans", "list_running_programs",
    "tomoscan_status", "ioc_status", "list_iocs",
    # PVs / motors / detector
    "read_pv", "read_motor", "check_pv_health", "check_motor_health",
    "resolve_pv", "list_pv_aliases",
    "check_detector_stream", "get_detector_image_stats",
    "view_detector_image",
    # data on disk
    "list_recent_scans", "read_scan_metadata",
    # machine / network status pages
    "list_status_pages", "fetch_url", "ping", "tcp_check", "dns_lookup",
    # docs
    "list_docs", "read_doc", "search_docs",
})

SLACK_PROMPT = """
# SLACK, READ-ONLY (overrides anything above about acting)

You are answering over Slack, for someone who is probably NOT at the
beamline. You can only LOOK: your tools are read-only. You cannot move
motors, write PVs, start/stop scans or IOCs, run shell commands or open
GUIs. If asked to do any of that, say it has to be done from the
beamline (pystream's AI Agent or the GUIs) — don't pretend.

For any status question ("is a scan running", "what's happening",
"is there beam", "is data being saved") call beamline_snapshot FIRST,
then drill down. pystream and XANES scans publish to
list_running_scans; TomoScan scans are read from tomoscan_status.

Answer in Slack mrkdwn: *bold* (single asterisks), `code`, short
bullet lists. No markdown tables, no # headings. Keep it short.
"""

_HISTORY_TURNS = 10


def _load_bridge_config() -> dict:
    cfg = {}
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f) or {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as ex:
        sys.exit(f"cannot read {CONFIG_FILE}: {ex}")
    cfg["bot_token"] = os.environ.get("SLACK_BOT_TOKEN") or cfg.get("bot_token")
    cfg["app_token"] = os.environ.get("SLACK_APP_TOKEN") or cfg.get("app_token")
    if not (cfg["bot_token"] and cfg["app_token"]):
        sys.exit(f"Slack tokens missing — set bot_token/app_token in "
                 f"{CONFIG_FILE} or SLACK_BOT_TOKEN/SLACK_APP_TOKEN.")
    cfg["allowed_users"] = set(cfg.get("allowed_users") or [])
    cfg["allowed_channels"] = set(cfg.get("allowed_channels") or [])
    return cfg


def _read_only_tool_ctx(tool_ctx: dict) -> dict:
    """Filter a full tool context down to READ_ONLY_TOOLS."""
    ctx = dict(tool_ctx)
    ctx["tool_specs_anthropic"] = [
        t for t in tool_ctx.get("tool_specs_anthropic", [])
        if t.get("name") in READ_ONLY_TOOLS]
    ctx["tool_specs_openai"] = [
        t for t in tool_ctx.get("tool_specs_openai", [])
        if (t.get("function") or {}).get("name") in READ_ONLY_TOOLS]
    full_get = tool_ctx.get("get_tool") or (lambda _n: None)

    def get_tool(name):
        if name not in READ_ONLY_TOOLS:
            return lambda **_kw: {"error": f"{name} is not available over "
                                           "Slack (read-only)."}
        return full_get(name)
    ctx["get_tool"] = get_tool
    ctx["write_tools"] = set()
    ctx["is_destructive"] = lambda _cmd: True
    return ctx


def _status_snapshot_text() -> str:
    """The active beamline's one-shot status text (no LLM)."""
    from .chat import _active_beamline_module
    mod = _active_beamline_module()
    hook = getattr(mod, "provide_agent_context", None) if mod else None
    ctx = (hook() or {}) if callable(hook) else {}
    fn = ctx.get("status_snapshot_text")
    if not callable(fn):
        return "This beamline doesn't provide a status snapshot — ask a question instead."
    return fn()


def _md_to_slack(text: str) -> str:
    """Light conversion of the model's markdown to Slack mrkdwn."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    return text


class SlackBridge:
    def __init__(self, cfg: dict):
        from slack_bolt import App
        self.cfg = cfg
        self.app = App(token=cfg["bot_token"])
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.history: dict[str, list] = {}     # thread key → messages
        self.history_proto: dict[str, str] = {}
        self.lock = threading.Lock()
        self.bot_user = self.app.client.auth_test()["user_id"]
        self.app.event("app_mention")(self._on_mention)
        self.app.event("message")(self._on_message)

    # ── access control ─────────────────────────────────────────────

    def _allowed(self, event: dict) -> bool:
        user, chan = event.get("user"), event.get("channel", "")
        if self.cfg["allowed_users"] and user not in self.cfg["allowed_users"]:
            return False
        is_dm = event.get("channel_type") == "im" or chan.startswith("D")
        if (self.cfg["allowed_channels"] and not is_dm
                and chan not in self.cfg["allowed_channels"]):
            return False
        return True

    # ── events ─────────────────────────────────────────────────────

    def _on_mention(self, event, say):
        self._handle(event)

    def _on_message(self, event, say):
        # Only DMs here; channel messages arrive via app_mention.
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        self._handle(event)

    def _handle(self, event: dict):
        if not self._allowed(event):
            LOGGER.info("ignored message from user=%s channel=%s",
                        event.get("user"), event.get("channel"))
            return
        text = re.sub(rf"<@{self.bot_user}>", "", event.get("text") or "").strip()
        if not text:
            return
        channel = event["channel"]
        thread = event.get("thread_ts") or event["ts"]
        self.pool.submit(self._answer, channel, thread, text)

    # ── answering ──────────────────────────────────────────────────

    def _post(self, channel, thread, text):
        return self.app.client.chat_postMessage(
            channel=channel, thread_ts=thread, text=text[:39000],
            unfurl_links=False)

    def _answer(self, channel: str, thread: str, text: str):
        try:
            if text.lower().strip(" ?!.") in ("status", "st", "s"):
                self._post(channel, thread, _status_snapshot_text())
                return
            self._agent_answer(channel, thread, text)
        except Exception as ex:
            LOGGER.exception("answer failed")
            try:
                self._post(channel, thread, f"⚠️ {type(ex).__name__}: {ex}")
            except Exception:
                pass

    def _agent_answer(self, channel: str, thread: str, text: str):
        from .chat import (build_agent_config, _chat_anthropic, _chat_openai,
                           _trim_history, _flatten_history, _to_history_message,
                           _is_turn_start,
                           PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI)
        from .subagents import WORKER_CTX

        cfg = build_agent_config()
        if cfg is None:
            self._post(channel, thread, "AI agent not configured (model / URL "
                       "missing in ~/.pystream/agent_settings.json).")
            return
        tool_ctx = _read_only_tool_ctx(cfg["tool_ctx"])
        system = cfg["system_prompt"].rstrip() + "\n\n" + SLACK_PROMPT
        proto = cfg["protocol"]
        key = f"{channel}:{thread}"
        with self.lock:
            hist = list(self.history.get(key, []))
            if self.history_proto.get(key) not in (None, proto):
                hist = _flatten_history(hist)
        hist = _trim_history(hist, _HISTORY_TURNS)

        thinking = self._post(channel, thread, "_looking…_")
        images: list = []
        last_edit = [0.0]

        def emit_tool(name, args, result):
            if result is None:
                now = time.monotonic()
                if now - last_edit[0] > 1.5:      # Slack rate limits
                    last_edit[0] = now
                    try:
                        self.app.client.chat_update(
                            channel=channel, ts=thinking["ts"],
                            text=f"_looking… ({name})_")
                    except Exception:
                        pass
            elif isinstance(result, dict) and result.get("image_base64"):
                images.append((name, result["image_base64"]))

        WORKER_CTX.emit_tool = emit_tool
        WORKER_CTX.confirm = lambda *_a: False
        turn: list = []
        try:
            fn = _chat_anthropic if proto == PROTOCOL_ANTHROPIC else _chat_openai
            if proto not in (PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI):
                raise ValueError(f"unknown protocol {proto!r}")
            answer, _usage = fn(cfg["url"], cfg["key"], cfg["model"], system,
                                hist, text, emit_tool, lambda *_a: False,
                                tool_ctx, turn_sink=turn)
        finally:
            for attr in ("emit_tool", "confirm"):
                if hasattr(WORKER_CTX, attr):
                    delattr(WORKER_CTX, attr)

        try:
            entries = [_to_history_message(m) for m in turn]
        except Exception:
            entries = []
        if not entries or not _is_turn_start(entries[0]):
            entries = [{"role": "user", "content": text},
                       {"role": "assistant", "content": answer or ""}]
        with self.lock:
            old = self.history.get(key, [])
            if self.history_proto.get(key) not in (None, proto):
                old = _flatten_history(old)
            self.history[key] = _trim_history(old + entries, _HISTORY_TURNS)
            self.history_proto[key] = proto

        self.app.client.chat_update(channel=channel, ts=thinking["ts"],
                                    text=_md_to_slack(answer or "(no answer)")[:39000])
        for name, b64 in images[:3]:
            import base64
            try:
                self.app.client.files_upload_v2(
                    channel=channel, thread_ts=thread,
                    content=base64.b64decode(b64),
                    filename=f"{name}_{time.strftime('%H%M%S')}.png",
                    title=name.replace("_", " "))
            except Exception as ex:
                LOGGER.warning("image upload failed: %s", ex)

    def run(self):
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        LOGGER.info("Slack bridge connected as %s (read-only, %d tools)",
                    self.bot_user, len(READ_ONLY_TOOLS))
        SocketModeHandler(self.app, self.cfg["app_token"]).start()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        import slack_bolt  # noqa: F401
    except ImportError:
        sys.exit("slack_bolt not installed: pip install 'beamline-agent[slack]'")
    SlackBridge(_load_bridge_config()).run()


if __name__ == "__main__":
    main()
