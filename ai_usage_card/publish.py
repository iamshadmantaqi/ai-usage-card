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


def _sync(clone: Path) -> None:
    """Bring the working clone level with the remote branch. If the branch does not exist yet,
    the first commit becomes its orphan root."""
    _git(clone, "fetch", "-q", "origin")
    remote = _git(clone, "branch", "-r", "--list", "origin/" + BRANCH).strip()
    if not remote:
        return
    if _git(clone, "rev-parse", "-q", "--verify", "HEAD", check=False).strip():
        _git(clone, "merge", "-q", "--ff-only", "origin/" + BRANCH)
    else:
        _git(clone, "checkout", "-q", "-B", BRANCH, "--track", "origin/" + BRANCH)


def publish(files: Dict[str, str], repo_url: str, clone: Path, credential_helper: Optional[str] = None) -> bool:
    """Write `files` into the clone, commit and push if any byte changed. Returns True if it pushed."""
    _ensure_clone(clone, repo_url, credential_helper)
    _sync(clone)
    names = sorted(files)
    for name in names:
        (clone / name).write_text(files[name], encoding="utf-8")
    if not _git(clone, "status", "--porcelain", "--", *names).strip():
        return False
    _git(clone, "add", "--", *names)
    _git(clone, "commit", "-q", "-m", COMMIT_MESSAGE)
    _git(clone, "push", "-q", "-u", "origin", BRANCH)
    return True
