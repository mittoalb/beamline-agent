"""Shared status registry for AI agents running across the beamline.

Any agent (pystream's Röntgen, a spawned reconstruction sub-agent, a
tomogui-batch worker on a GPU host, ...) publishes short "here's what
I'm doing" records to a single JSON file. pystream's Agents panel
watches the file and renders the live picture — who's running, what
they're doing, how they relate to each other.

**Registry file**: `~/.aps_agents/agents.json`, a dict of
    {agent_id: {name, kind, parent, host, state, activity, progress,
                started_ts, updated_ts, ttl_s}}

**Cross-machine**: `~/.aps_agents/` is on the user's home directory,
which at APS is NFS-mounted on every beamline host. Both pystream and
tomogui-batch (running on different machines under the same account)
write to the same file. If a site doesn't share home, an SSH-based
relay is trivial to add later — the registry format is the API.

**Publisher API**: `AgentStatusPublisher` is a context manager that
creates a record on enter, updates it on every `activity(...)` /
`progress(...)` call, and marks the record `done` (or `error` on an
unhandled exception) on exit. Drop-in for any agent:

    with AgentStatusPublisher(name="recon_subagent") as pub:
        pub.activity("dispatching to gpu01")
        ...
        pub.progress(3, 12, "recon 3/12")
        ...
        pub.finish("12 recons in 6m 40s")

For long-lived agents (pystream's Röntgen, which lives as long as
the app), don't use `with` — construct the publisher once, keep a
reference, call `.activity()` / `.waiting()` / `.finish()` explicitly.

**Parent-child relationships**: pystream sets the env var
`APS_AGENT_PARENT_ID` before spawning any sub-process; the sub-agent
picks it up as its `parent` by default. Panel uses this to render the
tree.

**TTL**: records without an update for `ttl_s` seconds are treated as
stale by the panel (agent likely crashed). Completed records linger
for `linger_s` seconds so the user sees "yes, that finished" before
they're purged.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import threading
import time
import weakref
from typing import Optional

APS_AGENTS_DIR   = os.path.expanduser("~/.aps_agents")
AGENTS_FILE      = os.path.join(APS_AGENTS_DIR, "agents.json")

DEFAULT_TTL_S    = 30      # a record older than this is "stale" to the panel
DEFAULT_LINGER_S = 300     # keep done/error records visible this long

LOGGER = logging.getLogger(__name__)

# Serialize writes across the process. Cross-process concurrency is
# handled by atomic tempfile+rename (loser wins; occasional lost
# intermediate update is fine — the next update overwrites it).
_write_lock = threading.Lock()

# Live publishers created in THIS process. atexit walks this set and
# calls close() on any that weren't finished manually, so a hard exit
# (window closed, SIGTERM after Qt aboutToQuit, ...) doesn't leave
# amber "stale" cards in every future pystream launch.
_live_publishers: "weakref.WeakSet[AgentStatusPublisher]" = weakref.WeakSet()


@atexit.register
def _close_all_publishers() -> None:
    # Snapshot into a list — closing mutates the WeakSet.
    for pub in list(_live_publishers):
        try:
            if not pub._closed:
                pub.finish("process exited")
        except Exception:
            pass


# ── low-level helpers ────────────────────────────────────────────────

def _hostname() -> str:
    try:
        return socket.gethostname().split(".")[0]
    except OSError:
        return "?"


def load_registry() -> dict:
    """Read the shared file. Empty dict on any error (missing file,
    corrupt JSON, permission)."""
    try:
        with open(AGENTS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_registry(reg: dict) -> None:
    """Atomic write via tempfile + rename. Multiple writers on the same
    filesystem race harmlessly — last writer wins; per-record loss is
    acceptable because the owning agent will publish another update
    shortly (or has finished, in which case its final state is what
    matters)."""
    with _write_lock:
        try:
            os.makedirs(APS_AGENTS_DIR, exist_ok=True)
            tmp = AGENTS_FILE + f".tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w") as f:
                json.dump(reg, f, indent=2, sort_keys=True)
            os.replace(tmp, AGENTS_FILE)
        except OSError as e:
            LOGGER.warning("failed to write %s: %s", AGENTS_FILE, e)


def update_record(record: dict) -> None:
    """Merge one agent's record into the registry. Preserves other
    agents' entries. `record` must have an `id` key."""
    if "id" not in record:
        raise ValueError("record must have an 'id' key")
    reg = load_registry()
    reg[record["id"]] = dict(record)
    _write_registry(reg)


def remove_record(agent_id: str) -> None:
    reg = load_registry()
    if reg.pop(agent_id, None) is not None:
        _write_registry(reg)


def purge_stale_records(stale_grace_s: float = 300.0) -> int:
    """Drop records that have been stuck in a non-terminal state without
    updates for `stale_grace_s` seconds — the owning process is very
    likely gone. Also drops done/error records past their linger. Writes
    the cleaned registry back atomically. Returns the number of records
    removed. Safe to call from any process; last writer wins.
    """
    reg = load_registry()
    now = time.time()
    keep: dict = {}
    dropped = 0
    for id_, rec in reg.items():
        if not isinstance(rec, dict):
            dropped += 1
            continue
        state = rec.get("state") or "?"
        updated = float(rec.get("updated_ts") or 0)
        age = now - updated
        if state in ("done", "error"):
            linger = float(rec.get("linger_s") or DEFAULT_LINGER_S)
            if age > linger:
                dropped += 1
                continue
        else:
            ttl = float(rec.get("ttl_s") or DEFAULT_TTL_S)
            # Stale-then-abandoned: past ttl_s (would render as "stale")
            # AND past the grace window on top → owner is gone.
            if age > ttl + stale_grace_s:
                dropped += 1
                continue
        keep[id_] = rec
    if dropped:
        _write_registry(keep)
    return dropped


# ── publisher ────────────────────────────────────────────────────────

class AgentStatusPublisher:
    """Publishes one agent's live state to the shared registry.

    Constructor args:
        name    — display name shown in the panel (e.g. "Röntgen")
        kind    — free-form category ("main", "subagent",
                  "tomogui-batch", "worker"). Panel uses this for
                  small visual differentiation only.
        parent  — parent agent's id, or None for a root agent.
                  Defaults to the APS_AGENT_PARENT_ID env var.
        host    — hostname; auto-detected via gethostname().
        ttl_s   — panel treats the record as stale after this many
                  seconds without update. Bump for slow-tick agents.
        linger_s — after finish()/error(), how long the record stays
                  visible in the panel before being purged.
        agent_id — override for the auto-generated id (only useful
                  when re-using a stable id across restarts).
    """

    def __init__(self,
                 name: str,
                 kind: str = "subagent",
                 parent: Optional[str] = None,
                 host: Optional[str] = None,
                 ttl_s: int = DEFAULT_TTL_S,
                 linger_s: int = DEFAULT_LINGER_S,
                 agent_id: Optional[str] = None):
        self.id = agent_id or (
            f"{name.replace(' ', '_')}-{_hostname()}-"
            f"{os.getpid()}-{int(time.time()*1000) & 0xffff:04x}"
        )
        self.name    = name
        self.kind    = kind
        self.parent  = parent if parent is not None else os.environ.get("APS_AGENT_PARENT_ID")
        self.host    = host or _hostname()
        self.ttl_s   = int(ttl_s)
        self.linger_s = int(linger_s)
        self._closed = False
        # Register for the process-exit sweep. Weak reference so
        # explicit deletion (e.g. widget cleanup) still frees us.
        _live_publishers.add(self)
        self._record = {
            "id":         self.id,
            "name":       self.name,
            "kind":       self.kind,
            "parent":     self.parent,
            "host":       self.host,
            "state":      "starting",
            "activity":   "",
            "progress":   None,
            "started_ts": time.time(),
            "updated_ts": time.time(),
            "ttl_s":      self.ttl_s,
            "linger_s":   self.linger_s,
        }

    # ── mutation methods ───────────────────────────────────────────

    def _push(self) -> None:
        self._record["updated_ts"] = time.time()
        try:
            update_record(self._record)
        except Exception as e:
            LOGGER.debug("agent-status push failed: %s", e)

    def activity(self, text: str) -> None:
        """Set state=running and update the activity line."""
        self._record["state"] = "running"
        self._record["activity"] = str(text)
        self._push()

    def waiting(self, text: str = "") -> None:
        """Set state=waiting (blocked on user / IO / peer). Optional
        activity text describing what for."""
        self._record["state"] = "waiting"
        if text:
            self._record["activity"] = str(text)
        self._push()

    def idle(self, text: str = "idle") -> None:
        """Set state=idle (agent is alive but has nothing to do)."""
        self._record["state"] = "idle"
        self._record["activity"] = str(text)
        self._push()

    def progress(self, done: int, total: int, text: str = "") -> None:
        """Attach a progress fraction to the current activity."""
        try:
            self._record["progress"] = {"done": int(done), "total": max(1, int(total))}
        except (TypeError, ValueError):
            self._record["progress"] = None
        if text:
            self._record["activity"] = str(text)
        self._push()

    def finish(self, summary: str = "") -> None:
        """Mark done. Record lingers `linger_s` before the panel purges it."""
        self._record["state"] = "done"
        if summary:
            self._record["activity"] = str(summary)
        self._record["progress"] = None
        self._push()
        self._closed = True

    def error(self, text: str) -> None:
        """Mark failed. Record lingers `linger_s` before purge."""
        self._record["state"] = "error"
        self._record["activity"] = str(text)
        self._push()
        self._closed = True

    def close(self) -> None:
        """Force-finish if the caller didn't call finish/error."""
        if not self._closed:
            self.finish()

    # ── context-manager ────────────────────────────────────────────

    def __enter__(self) -> "AgentStatusPublisher":
        # Publish an initial "running" so the panel sees the record.
        self.activity(self._record.get("activity") or "starting")
        return self

    def __exit__(self, exc_type, exc_val, _exc_tb) -> bool:
        if exc_type is not None:
            self.error(f"{exc_type.__name__}: {exc_val}")
        elif not self._closed:
            self.finish()
        return False   # never swallow exceptions


# ── helper for spawned children ──────────────────────────────────────

def child_env(parent_id: str) -> dict:
    """Return an os.environ overlay that a spawned subprocess should
    inherit so its AgentStatusPublisher(parent=None) auto-attaches
    to `parent_id`. Use as:

        subprocess.Popen([...], env={**os.environ, **child_env(pub.id)})
    """
    return {"APS_AGENT_PARENT_ID": str(parent_id)}
