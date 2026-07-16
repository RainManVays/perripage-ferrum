from __future__ import annotations

import subprocess
from functools import lru_cache
from importlib import metadata
from pathlib import Path


@lru_cache(maxsize=1)
def app_version() -> str:
    """pyproject.toml's version plus the short git commit hash, e.g.
    "0.1.0 (a3c9ea1)" — shown in the window title so a running instance's
    exact code state is visible at a glance, without having to ask
    ("вы точно перезапустили после фикса?"). The version alone doesn't
    change often enough between commits during active development to be
    useful on its own. Both pieces are best-effort: an installed wheel
    with no .git directory just shows the plain version, no crash."""
    try:
        version = metadata.version("periprint")
    except metadata.PackageNotFoundError:
        version = "0.0.0"

    commit = _short_git_commit()
    return f"{version} ({commit})" if commit else version


def _short_git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
            # cwd must be inside the repo so `git` finds the right .git dir
            # — an installed console-script's own process cwd could be
            # anywhere. An editable install (`pip install -e .`, how this
            # project is always run — see feedback_venv_and_sudo) keeps
            # this file inside the real repo tree; a non-editable wheel
            # install has no .git at all here, which just falls through to
            # the OSError/SubprocessError branch below (no crash).
            cwd=Path(__file__).resolve().parent,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None
