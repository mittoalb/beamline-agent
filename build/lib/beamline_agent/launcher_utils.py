"""Helpers for beamline-plugin LAUNCHERS that need to spawn a Python
script in a **different conda env** than the one pystream itself is
running in.

The problem: most launcher plugins in `beamlines/*/*.py` do

    subprocess.Popen([sys.executable, script_path], ...)

which runs the script under pystream's env. If the target tool has
imports that aren't in pystream's env (e.g. `atomo` needs its own
env with adaptive-exposure deps; `tomogui-cli` needs `tomoguiAI`),
this fails at import time and the launcher looks like it "silently
didn't work".

The fix: plugins declare their target env (either as `CONDA_ENV`
class attribute or as an argument to `spawn_python_in_env` below).
This module wraps `conda run -n <env> python <script>` in a way that
works over any shell (including tcsh) and doesn't require login-shell
init.

Usage pattern in a launcher:

    from ...launcher_utils import spawn_python_in_env
    ...
    proc = spawn_python_in_env(
        env=getattr(cls, "CONDA_ENV", None),
        script_path=script_path,
        script_args=[],
        cwd=os.path.dirname(script_path),
    )

If `env` is None or empty, falls back to `sys.executable` — old
behaviour, preserved so plugins that were already correct don't
change.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Sequence

LOGGER = logging.getLogger(__name__)


def _find_conda() -> str | None:
    """Locate the `conda` executable. Prefers the one adjacent to the
    running python (typical for a properly-managed conda install);
    falls back to PATH lookup; None if neither works."""
    # 1. Adjacent to sys.executable — envs/<env>/bin/python → conda at
    #    the base install's bin/conda.
    py = os.path.realpath(sys.executable)
    d = os.path.dirname(py)
    for _ in range(5):
        cand = os.path.join(d, "conda")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # 2. PATH
    return shutil.which("conda")


def spawn_python_in_env(
    env: str | None,
    script_path: str,
    *,
    script_args: Sequence[str] = (),
    cwd: str | None = None,
    logger: logging.Logger | None = None,
    detach: bool = True,
) -> subprocess.Popen:
    """Spawn `python script_path *script_args` in the named conda env.

    When `env` is falsy → runs `[sys.executable, script_path, *args]`
    (the pre-existing behaviour for launchers that don't need an env
    switch).

    When `env` is set → runs `conda run -n <env> --no-capture-output
    python <script> <args>` so the subprocess inherits `env`'s
    interpreter + site-packages. `--no-capture-output` keeps the
    child's stdout/stderr flowing to the user's terminal.

    `detach=True` (default) sets `start_new_session=True` so the child
    survives if the parent (pystream) exits.

    Returns the `subprocess.Popen` handle. Never raises for missing
    conda — falls back to sys.executable with a warning."""
    log = logger or LOGGER
    common_kwargs = {"cwd": cwd}
    if detach:
        common_kwargs["start_new_session"] = True

    if env:
        conda = _find_conda()
        if conda is None:
            log.warning("spawn_python_in_env(env=%r): `conda` not found, "
                        "falling back to sys.executable", env)
        else:
            argv = [conda, "run", "-n", env, "--no-capture-output",
                    "python", script_path, *script_args]
            log.info("Launching: %s", " ".join(argv))
            return subprocess.Popen(argv, **common_kwargs)

    argv = [sys.executable, script_path, *script_args]
    log.info("Launching (host env): %s", " ".join(argv))
    return subprocess.Popen(argv, **common_kwargs)


def spawn_command_in_env(
    env: str | None,
    command: str,
    *,
    cwd: str | None = None,
    logger: logging.Logger | None = None,
    detach: bool = True,
) -> subprocess.Popen:
    """Like `spawn_python_in_env` but for arbitrary commands (not
    necessarily python). `command` is a shell string; runs under
    `conda run -n <env> bash -c '<command>'`. Fallback: plain
    `bash -c` in host env.

    Use for launcher plugins that shell out to a CLI like `bl_gui`,
    `tomogui-cli`, `tomocupy`, etc."""
    log = logger or LOGGER
    common_kwargs = {"cwd": cwd}
    if detach:
        common_kwargs["start_new_session"] = True

    if env:
        conda = _find_conda()
        if conda is None:
            log.warning("spawn_command_in_env(env=%r): `conda` not found, "
                        "falling back to host env", env)
        else:
            argv = [conda, "run", "-n", env, "--no-capture-output",
                    "bash", "-c", command]
            log.info("Launching: %s", " ".join(argv))
            return subprocess.Popen(argv, **common_kwargs)

    argv = ["bash", "-c", command]
    log.info("Launching (host env): %s", " ".join(argv))
    return subprocess.Popen(argv, **common_kwargs)
