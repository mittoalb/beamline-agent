"""Core agent-context bootstrap.

Every markdown file shipped inside the pystream package under
`agent/context_docs/*.md` is copied on startup into
`~/.pystream/docs/` where the agent's existing `read_file` /
`bash: cat` tools can find it.

Copy semantics:
- If the destination doesn't exist → copy from the package.
- If the destination exists AND is a regular file → **do not
  touch**. The user may have edited it; their edits are preserved.
- If the destination exists AND is a symlink from an older
  install layout → replace with the packaged copy (the old
  ~/Software/*/AGENTS.md-scanning approach is dead).

Deploy story: `pip install pystream` puts the .md files into the
installed package. First launch on any machine bootstraps them into
that user's `~/.pystream/docs/`. Nothing on the target machine
needs to have tomogui (or any other project) installed for the
agent to have the right instructions.
"""

from __future__ import annotations

import glob
import importlib
import json
import logging
import os
import shutil

# Host state directory. Defaults to ~/.pystream for backward compat.
PYSTREAM_HOME = os.path.expanduser("~/.pystream")

DOCS_DIR             = os.path.join(PYSTREAM_HOME, "docs")
PACKAGED_DOCS_DIR    = os.path.join(os.path.dirname(__file__), "context_docs")

# Procedures runtime tree — mirrors any package that exposes
# `data_dir()`. See `_bootstrap_configured_packages` below.
PROCEDURES_DIR = os.path.join(PYSTREAM_HOME, "procedures")

# User-editable list of Python packages the agent should probe on
# startup. First-launch creates an empty template with just a
# comment explaining the format; the user adds the packages they
# want the agent aware of. Nothing is hardcoded inside beamline-
# agent — this file is the single source of truth.
AGENT_PACKAGES_FILE = os.path.join(PYSTREAM_HOME, "agent_packages.json")

LOGGER = logging.getLogger(__name__)


def bootstrap_agent_context_docs() -> None:
    """First-launch discovery pass. Runs three things in order:

    1. Copies every packaged `.md` from `beamline_agent/context_docs/`
       into `~/.pystream/docs/` (short quick-reference docs).
    2. Cleans up legacy symlinks from the old
       `~/Software/*/AGENTS.md` scanner (defunct approach).
    3. Reads `~/.pystream/agent_packages.json` and probes each
       listed package for `data_dir()` (mirror into
       `~/.pystream/procedures/`) or `AGENTS.md` at repo root
       (copy to `~/.pystream/docs/<pkg>_AGENTS.md`).

    Never overwrites a regular file at the destination — user edits
    always win. Idempotent."""
    try:
        os.makedirs(DOCS_DIR, exist_ok=True)
    except OSError as e:
        LOGGER.debug("cannot create %s: %s", DOCS_DIR, e)
        return

    if not os.path.isdir(PACKAGED_DOCS_DIR):
        LOGGER.debug("no packaged docs at %s", PACKAGED_DOCS_DIR)
        return

    _drop_legacy_symlinks()

    # Top-level docs (pystream.md, tomogui.md, bl_gui.md, ...). Skip
    # anything inside `procedures/` — that gets its own recursive
    # copy to a different destination below.
    for src in sorted(glob.glob(os.path.join(PACKAGED_DOCS_DIR, "*.md"))):
        name = os.path.basename(src)
        dst = os.path.join(DOCS_DIR, name)
        if os.path.isfile(dst) and not os.path.islink(dst):
            continue
        try:
            if os.path.islink(dst):
                os.unlink(dst)
            shutil.copyfile(src, dst)
        except OSError as e:
            LOGGER.debug("failed to install %s → %s: %s", src, dst, e)

    # Configured packages — the user's list at
    # `~/.pystream/agent_packages.json`. Each package is probed for
    # conventions (data_dir() → procedures tree, AGENTS.md at root
    # → tool doc). Missing packages are skipped silently.
    _bootstrap_configured_packages()


def _load_agent_packages() -> list[str]:
    """Read the user's `agent_packages.json`. Create an empty
    template the first time it's missing. Returns the effective
    package list (never raises — returns [] on any read error, so
    the agent runs with zero configured packages until the user
    fixes the file)."""
    if not os.path.isfile(AGENT_PACKAGES_FILE):
        _write_empty_agent_packages_template()
    try:
        with open(AGENT_PACKAGES_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        LOGGER.warning("cannot read %s (%s) — no packages will be discovered",
                       AGENT_PACKAGES_FILE, e)
        return []
    pkgs = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(pkgs, list):
        LOGGER.warning("%s missing top-level 'packages' list — no packages "
                       "will be discovered", AGENT_PACKAGES_FILE)
        return []
    return [str(p) for p in pkgs if isinstance(p, str) and p.strip()]


def copy_example_agent_packages(overwrite: bool = False) -> str:
    """Copy the packaged example `agent_packages.json` template into
    `~/.pystream/`. Returns the destination path.

    By default this refuses to overwrite an existing file — call with
    `overwrite=True` to replace whatever's there. Convenient
    entry-point for `python -c "from beamline_agent.context import
    copy_example_agent_packages; copy_example_agent_packages()"`.

    Raises `FileNotFoundError` if the package's `examples/` bundle
    is missing (should never happen from a normal install)."""
    src = os.path.join(os.path.dirname(__file__),
                       "examples", "agent_packages.json")
    if not os.path.isfile(src):
        raise FileNotFoundError(
            f"example config not shipped with this install: {src}")
    if os.path.exists(AGENT_PACKAGES_FILE) and not overwrite:
        raise FileExistsError(
            f"{AGENT_PACKAGES_FILE} already exists — pass overwrite=True "
            "to replace it, or edit the existing file directly.")
    os.makedirs(PYSTREAM_HOME, exist_ok=True)
    shutil.copyfile(src, AGENT_PACKAGES_FILE)
    return AGENT_PACKAGES_FILE


def _write_empty_agent_packages_template() -> None:
    """Seed `~/.pystream/agent_packages.json` with an EMPTY packages
    list plus a comment explaining the format the first time it's
    missing. Never overwrites an existing file. Nothing about which
    packages to look at is hardcoded — this template is a starting
    point; the user fills in their own list."""
    try:
        os.makedirs(PYSTREAM_HOME, exist_ok=True)
    except OSError as e:
        LOGGER.debug("cannot create %s: %s", PYSTREAM_HOME, e)
        return
    payload = {
        "_comment": (
            "List of Python packages beamline-agent should discover at "
            "startup. Each package is probed by convention: a `data_dir()` "
            "function → its tree is mirrored into ~/.pystream/procedures/; "
            "an `AGENTS.md` at the repo root → copied to "
            "~/.pystream/docs/<pkg>_AGENTS.md. Missing packages are "
            "skipped silently, so a superset list is safe. Add package "
            "names below to expose them to the agent — e.g. \"pystream\", "
            "\"bl_gui\", \"xanes_gui\", \"tomogui\", \"beamlines_procedures\"."
        ),
        "packages": [],
    }
    try:
        with open(AGENT_PACKAGES_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        LOGGER.info("wrote empty %s template — edit it to add packages",
                    AGENT_PACKAGES_FILE)
    except OSError as e:
        LOGGER.debug("cannot write %s: %s", AGENT_PACKAGES_FILE, e)


def _bootstrap_configured_packages() -> None:
    """For each package in `agent_packages.json`, try to import it,
    then run whichever discovery conventions match:

      1. `<pkg>.data_dir()` returns a directory → recursively mirror
         its `.md` files into `~/.pystream/procedures/` (procedures
         package pattern, e.g. `beamlines_procedures`).
      2. An `AGENTS.md` at or above `<pkg>.__file__` → copy to
         `~/.pystream/docs/<pkg>.md` so the agent can `read_file`
         it via a predictable path.

    Missing packages are skipped silently. Existing destination files
    are preserved (user edits win) — same semantics as the top-level
    docs bootstrap."""
    for pkg_name in _load_agent_packages():
        try:
            mod = importlib.import_module(pkg_name)
        except Exception as e:
            LOGGER.debug("agent-package %r not importable: %s", pkg_name, e)
            continue
        _try_bootstrap_procedures_from(mod)
        _try_bootstrap_agents_md_from(mod)


def _try_bootstrap_procedures_from(mod) -> None:
    """If `mod.data_dir()` returns a directory, recursively mirror
    its `.md` files into `~/.pystream/procedures/`."""
    data_dir = getattr(mod, "data_dir", None)
    if not callable(data_dir):
        return
    try:
        src_dir = data_dir()
    except Exception as e:
        LOGGER.debug("%s.data_dir() failed: %s", mod.__name__, e)
        return
    if not isinstance(src_dir, str) or not os.path.isdir(src_dir):
        return
    try:
        os.makedirs(PROCEDURES_DIR, exist_ok=True)
    except OSError as e:
        LOGGER.debug("cannot create %s: %s", PROCEDURES_DIR, e)
        return
    for root, _dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        dst_root = (PROCEDURES_DIR if rel_root == "."
                    else os.path.join(PROCEDURES_DIR, rel_root))
        try:
            os.makedirs(dst_root, exist_ok=True)
        except OSError as e:
            LOGGER.debug("cannot create %s: %s", dst_root, e)
            continue
        for fname in files:
            if not fname.endswith(".md"):
                continue
            src = os.path.join(root, fname)
            dst = os.path.join(dst_root, fname)
            if os.path.isfile(dst) and not os.path.islink(dst):
                continue
            try:
                if os.path.islink(dst):
                    os.unlink(dst)
                shutil.copyfile(src, dst)
            except OSError as e:
                LOGGER.debug("failed to install %s → %s: %s", src, dst, e)


def _try_bootstrap_agents_md_from(mod) -> None:
    """Look for `AGENTS.md` next to `mod.__file__` or up to 4 levels
    above (matches editable installs where the package sits under
    `src/<pkg>/` and AGENTS.md is at the repo root). If found, copy
    to `~/.pystream/docs/<pkg>_AGENTS.md`.

    The `_AGENTS.md` suffix is deliberate: it avoids clashing with
    beamline-agent's own quick-reference doc at `context_docs/<pkg>.md`
    (the two are complementary — the shipped one is a short
    action-first summary; the discovered one is the tool's full deep
    reference)."""
    pkg_name = mod.__name__
    try:
        pkg_dir = os.path.dirname(os.path.abspath(mod.__file__))
    except Exception:
        return
    candidate = None
    d = pkg_dir
    for _ in range(5):  # <=4 levels up, plus the pkg dir itself
        p = os.path.join(d, "AGENTS.md")
        if os.path.isfile(p):
            candidate = p
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if candidate is None:
        return
    dst = os.path.join(DOCS_DIR, f"{pkg_name}_AGENTS.md")
    if os.path.isfile(dst) and not os.path.islink(dst):
        # User has a hand-edited copy under DOCS_DIR — preserve it.
        return
    try:
        os.makedirs(DOCS_DIR, exist_ok=True)
        if os.path.islink(dst):
            os.unlink(dst)
        shutil.copyfile(candidate, dst)
    except OSError as e:
        LOGGER.debug("failed to install %s → %s: %s", candidate, dst, e)


def _drop_legacy_symlinks() -> None:
    """Remove symlinks left over from the old ~/Software/*/AGENTS.md
    scanner (files named like `<project>_AGENTS.md` in DOCS_DIR).
    They pointed at machine-specific paths that break on deploy."""
    for entry in glob.glob(os.path.join(DOCS_DIR, "*_AGENTS.md")):
        try:
            if os.path.islink(entry):
                os.unlink(entry)
        except OSError as e:
            LOGGER.debug("could not unlink legacy %s: %s", entry, e)
    # Also legacy README-style symlinks the same scanner produced.
    for entry in glob.glob(os.path.join(DOCS_DIR, "*_README.md")):
        try:
            if os.path.islink(entry):
                os.unlink(entry)
        except OSError as e:
            LOGGER.debug("could not unlink legacy %s: %s", entry, e)
