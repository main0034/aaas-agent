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


def test_isolate_at_an_older_commit(tmp_path: Path) -> None:
    """--at: the checkout is master as it was, and the newer commit is unreachable."""
    import subprocess as sp

    def g(*a: str, cwd: Path) -> str:
        return sp.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    repo = tmp_path / "r"
    repo.mkdir()
    g("init", "-q", "-b", "master", cwd=repo)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        g("config", k, v, cwd=repo)
    (repo / "a").write_text("1")
    g("add", ".", cwd=repo)
    g("commit", "-qm", "old", cwd=repo)
    old = g("rev-parse", "HEAD", cwd=repo)
    (repo / "a").write_text("2")
    g("commit", "-qam", "new", cwd=repo)
    new = g("rev-parse", "HEAD", cwd=repo)
    g("checkout", "-q", "-B", "master", old, cwd=repo)
    g("remote", "add", "origin", str(tmp_path / "nowhere"), cwd=repo)
    from harness import workspace as ws

    ws.isolate(repo)
    assert g("rev-parse", "HEAD", cwd=repo) == old
    assert sp.run(["git", "cat-file", "-e", new], cwd=repo).returncode != 0
