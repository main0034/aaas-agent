"""
Builds the agent's working tree.

Deliberately clones from GitHub rather than mounting the local checkouts.

Two reasons, and the second is the one that matters:

  1. The agent works from origin state, so a run is reproducible and cannot be
     accidentally influenced by whatever branch happens to be checked out on
     the laptop.
  2. A failed or confused run cannot dirty a working tree that has real work in
     it. The blast radius of a bad run is a directory under runs/ and a branch
     on GitHub, both of which are cheap to delete.

The modules repo is cloned as *reference*: the deployment runbook tells the
agent to read `modules/app-stack/README.md`, and the policy layer refuses to
write anywhere inside it.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_USER_NAME = "aaas-agent"
GIT_USER_EMAIL = "aaas-agent@users.noreply.github.com"


class WorkspaceError(RuntimeError):
    pass


@dataclass
class Workspace:
    root: Path
    deployments: Path
    reference: Path
    # The application checkout, for the create-app task. None for create-deployment.
    app: Path | None = None

    @property
    def writable_roots(self) -> list[Path]:
        # Exactly one checkout is writable per task. create-app writes the
        # application and only reads the deployments repo (for the runbook and
        # PROMPT.md); create-deployment is the other way round. Within the
        # writable checkout, policy.py still refuses the guardrail paths.
        if self.app is not None:
            return [self.app]
        return [self.deployments]


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WorkspaceError(
            f"`{' '.join(cmd)}` failed ({result.returncode}):\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def _clone(owner: str, repo: str, dest: Path) -> Path:
    """Clone with `gh` so the token in GH_TOKEN is used and never written to disk.

    `gh repo clone` authenticates the clone and nothing after it: the checkout
    has no credential helper, so the runbook's `git push` fails with "could not
    read Username". The first create-app run spent five turns on that and ended
    by pushing to a URL with ${GH_TOKEN} spliced into it (FINDINGS.md #21). The
    fix is here, not in the agent: point git at gh as the helper, per repository,
    so the token is read from the environment at push time and never lands in a
    URL, an argument list or a config file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["gh", "repo", "clone", f"{owner}/{repo}", str(dest), "--", "--quiet"])
    _run(["git", "config", "--local", "credential.https://github.com.helper", ""], cwd=dest)
    _run(
        ["git", "config", "--local", "--add", "credential.https://github.com.helper",
         "!gh auth git-credential"],
        cwd=dest,
    )
    return dest


def prepare(
    root: Path,
    owner: str,
    deployments_repo: str = "aaas-deployments",
    modules_repo: str = "aaas-infra-modules",
    app_repo: str | None = None,
    isolate_app: bool = False,
) -> Workspace:
    if not os.environ.get("GH_TOKEN"):
        raise WorkspaceError(
            "GH_TOKEN is not set. The agent needs a token that can push a branch and "
            "open a pull request on the deployments repo, and nothing else. See the "
            "README - a fine-grained PAT scoped to that one repository is the right "
            "shape for the POC."
        )

    root.mkdir(parents=True, exist_ok=True)

    deployments = _clone(owner, deployments_repo, root / deployments_repo)
    _run(["git", "config", "user.name", GIT_USER_NAME], cwd=deployments)
    _run(["git", "config", "user.email", GIT_USER_EMAIL], cwd=deployments)

    reference_root = root / "reference"
    reference_root.mkdir(parents=True, exist_ok=True)
    _clone(owner, modules_repo, reference_root / modules_repo)

    app = None
    if app_repo:
        # The repository must already exist - create-app.md section 0. Cloning
        # fails legibly here if it does not, before the agent spends a turn.
        app = _clone(owner, app_repo, root / app_repo)
        if isolate_app:
            isolate(app)
        _run(["git", "config", "user.name", GIT_USER_NAME], cwd=app)
        _run(["git", "config", "user.email", GIT_USER_EMAIL], cwd=app)

    return Workspace(root=root, deployments=deployments, reference=reference_root, app=app)


def clone_isolated(dest: Path, owner: str, app_repo: str) -> Path:
    """A second, isolated checkout of the app's default branch, for the spec-tester.

    create-change runs both roles in one container; the builder's checkout is the
    normal one from prepare(), and this one has no remote and no other refs.
    """
    app = _clone(owner, app_repo, dest)
    isolate(app)
    _run(["git", "config", "user.name", GIT_USER_NAME], cwd=app)
    _run(["git", "config", "user.email", GIT_USER_EMAIL], cwd=app)
    return app


def isolate(repo: Path) -> None:
    """Cut a checkout off from everything but the default branch's current commit.

    The write-acceptance task writes tests for a change someone else is building, and
    the measurement is worthless if it can read that change. The app repositories are
    public and a normal clone carries every branch (`origin/feat/...`), so: drop every
    other ref, then the remote itself, so nothing can be fetched by name either. The
    session also gets no token and no `gh`, and the policy refuses git's network
    subcommands; this function is the part that does not depend on the policy.
    """
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    _run(["git", "remote", "remove", "origin"], cwd=repo)
    refs = _run(["git", "for-each-ref", "--format=%(refname)"], cwd=repo).splitlines()
    for ref in refs:
        if ref != f"refs/heads/{branch}":
            _run(["git", "update-ref", "-d", ref], cwd=repo)
    _run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=repo)
    _run(["git", "gc", "--prune=now", "--quiet"], cwd=repo)


def head_sha(repo: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], cwd=repo)
