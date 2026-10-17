"""Commit the SVGs to a dedicated orphan branch of the profile repo, and push only when they changed."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

GIT = "/usr/bin/git"
BRANCH = "usage-card"
COMMIT_MESSAGE = "Update AI usage card"


class PublishError(Exception):
    pass


def _git(clone: Path, *args: str, check: bool = True) -> str:
    # launchd gives jobs a bare PATH; Homebrew's gh is the credential helper.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    result = subprocess.run([GIT] + list(args), cwd=str(clone), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    if check and result.returncode != 0:
        raise PublishError("git %s failed: %s" % (args[0], result.stderr.strip()))
    return result.stdout


def _identity(name: str) -> Optional[str]:
    result = subprocess.run([GIT, "config", "--global", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return result.stdout.strip() or None


def _ensure_clone(clone: Path, repo_url: str, credential_helper: Optional[str]) -> None:
    if (clone / ".git").exists():
        return
    clone.mkdir(parents=True, exist_ok=True)
    _git(clone, "init", "-q", "-b", BRANCH)
    _git(clone, "remote", "add", "origin", repo_url)
    if credential_helper:
        _git(clone, "config", "--add", "credential.helper", "")  # reset the list: this helper only
        _git(clone, "config", "--add", "credential.helper", credential_helper)
    for key, name in (("user.name", "user.name"), ("user.email", "user.email")):
        value = _identity(name)
        if value:
            _git(clone, "config", key, value)


def _sync(clone: Path) -> bool:
    """Put the working clone on the remote branch, and say whether the remote branch exists. If it
    does not exist yet, the first commit becomes its orphan root.

    The remote is taken as it is, even when it was rebuilt or moved on from another clone, and any
    half-written files from a failed run are dropped: this clone holds only generated files, and
    all of them are written again straight after. A fast-forward merge here would stop every later
    run on either of those states."""
    _git(clone, "fetch", "-q", "--prune", "origin")  # --prune: a deleted remote branch is seen as gone
    if not _git(clone, "branch", "-r", "--list", "origin/" + BRANCH).strip():
        return False
    _git(clone, "checkout", "-q", "-f", "-B", BRANCH, "--track", "origin/" + BRANCH)
    return True


def _unpushed(clone: Path, remote_exists: bool) -> bool:
    """A local commit the remote branch lacks: the remote branch was never created (an earlier push
    failed) or was deleted. When the remote branch exists, `_sync` has already put HEAD on it."""
    return not remote_exists and bool(_git(clone, "rev-parse", "-q", "--verify", "HEAD", check=False).strip())


def publish(files: Dict[str, str], repo_url: str, clone: Path, credential_helper: Optional[str] = None) -> bool:
    """Write `files` into the clone, commit if any byte changed, and push whatever the remote
    branch lacks. Returns True if it pushed."""
    _ensure_clone(clone, repo_url, credential_helper)
    remote_exists = _sync(clone)
    names = sorted(files)
    for name in names:
        (clone / name).write_text(files[name], encoding="utf-8")
    if _git(clone, "status", "--porcelain", "--", *names).strip():
        _git(clone, "add", "--", *names)
        _git(clone, "commit", "-q", "-m", COMMIT_MESSAGE)
    elif not _unpushed(clone, remote_exists):
        return False
    _git(clone, "push", "-q", "-u", "origin", "%s:refs/heads/%s" % (BRANCH, BRANCH))
    return True
