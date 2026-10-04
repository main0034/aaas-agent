"""workspace.isolate: after it, the checkout cannot name or fetch any other branch."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.workspace import isolate


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_isolate_leaves_only_the_current_branch(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "master", cwd=origin)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base", cwd=origin)
    git("checkout", "-q", "-b", "feat/secret", cwd=origin)
    (origin / "Secret.cs").write_text("the implementation")
    git("add", ".", cwd=origin)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "impl", cwd=origin)
    secret = git("rev-parse", "HEAD", cwd=origin)
    git("checkout", "-q", "master", cwd=origin)

    clone = tmp_path / "clone"
    git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    assert "origin/feat/secret" in git("branch", "-a", cwd=clone)

    isolate(clone)

    assert git("for-each-ref", "--format=%(refname)", cwd=clone) == "refs/heads/master"
    assert git("remote", cwd=clone) == ""
    missing = subprocess.run(["git", "cat-file", "-e", secret], cwd=clone, capture_output=True)
    assert missing.returncode != 0
