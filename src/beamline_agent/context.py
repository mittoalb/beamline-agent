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
import logging
import os
import shutil

# Host state directory. Defaults to ~/.pystream for backward compat.
PYSTREAM_HOME = os.path.expanduser("~/.pystream")

DOCS_DIR             = os.path.join(PYSTREAM_HOME, "docs")
PACKAGED_DOCS_DIR    = os.path.join(os.path.dirname(__file__), "context_docs")

LOGGER = logging.getLogger(__name__)


def bootstrap_agent_context_docs() -> None:
    """Copy every packaged `.md` from `agent/context_docs/` into
    `~/.pystream/docs/`. Never overwrites a regular file at the
    destination (preserves user edits). Idempotent.

    Also cleans up legacy symlinks from the older
    `~/Software/*/AGENTS.md` scanner — those pointed at
    machine-specific paths and don't survive a deploy to another
    host, so they're replaced with the packaged copy."""
    try:
        os.makedirs(DOCS_DIR, exist_ok=True)
    except OSError as e:
        LOGGER.debug("cannot create %s: %s", DOCS_DIR, e)
        return

    if not os.path.isdir(PACKAGED_DOCS_DIR):
        LOGGER.debug("no packaged docs at %s", PACKAGED_DOCS_DIR)
        return

    _drop_legacy_symlinks()

    for src in sorted(glob.glob(os.path.join(PACKAGED_DOCS_DIR, "*.md"))):
        name = os.path.basename(src)
        dst = os.path.join(DOCS_DIR, name)
        # Regular file at dst → user copy, preserve.
        if os.path.isfile(dst) and not os.path.islink(dst):
            continue
        try:
            # Symlink or missing → (re)create as a fresh copy from
            # the package. Copy rather than symlink so an updated
            # pip install can re-bootstrap cleanly.
            if os.path.islink(dst):
                os.unlink(dst)
            shutil.copyfile(src, dst)
        except OSError as e:
            LOGGER.debug("failed to install %s → %s: %s", src, dst, e)


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
