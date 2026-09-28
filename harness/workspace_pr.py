"""
Put an existing checkout onto a pull request's branch, for a fix round.

Kept apart from workspace.py's cloning because it runs between sessions, on a
checkout the previous session may have left in any state: half-staged files,
build output, a local commit it never pushed. The fix round starts from what CI
saw - origin's branch head - and nothing else.
"""

from __future__ import annotations

from pathlib import Path

from .workspace import _run


def checkout_pr_branch(repo: Path, branch: str, sha: str) -> None:
    _run(["git", "fetch", "--quiet", "origin", branch], cwd=repo)
    _run(["git", "checkout", "--quiet", "-B", branch, sha], cwd=repo)
    _run(["git", "reset", "--quiet", "--hard", sha], cwd=repo)
    _run(["git", "clean", "-fdxq"], cwd=repo)
    _run(["git", "branch", "--quiet", f"--set-upstream-to=origin/{branch}", branch], cwd=repo)


def diff_stat(repo: Path, base: str) -> str:
    _run(["git", "fetch", "--quiet", "origin", base], cwd=repo)
    return _run(["git", "diff", "--stat", f"origin/{base}...HEAD"], cwd=repo)
