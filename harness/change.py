"""
A change record: the request and its acceptance tests, kept in the app repository.

Roadmap step 2b. Every change to an application gets a directory,
`changes/<id>/`, on its own branch:

  1. the harness commits the request as `changes/<id>/brief.md` and pushes the
     branch - before anyone writes code
  2. the spec-tester writes acceptance tests from the request, in an isolated
     checkout of master (workspace.isolate)
  3. the builder writes the change on that branch and opens the pull request,
     never having seen the tests
  4. the harness commits the spec-tester's tests into `changes/<id>/` and pushes;
     the test project compiles `changes/**/*.cs`, so CI's endpoint tests run them
  5. fix rounds may read them and may not change them

After the squash merge, `master` carries the request, the code and the tests that
accepted it in one commit, and the tests keep running as regression tests.

Every commit the harness makes under `changes/` carries the trailer
`AaaS-Change: <id>`. The app's `scripts/check-changes.sh` fails a pull request in
which any other commit touches `changes/`, and `verify_untouched` checks the same
thing from here before a run reports green. Neither stops a hostile agent that
rewrites history; both stop the honest failure finding 25 measured, a fix round
editing an assertion until CI agrees.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

from .workspace import _run

TRAILER = "AaaS-Change"
CHANGES_DIR = Path("changes")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ChangeError(RuntimeError):
    pass


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if not slug:
        raise ChangeError(f"Cannot make a change id from {text!r}")
    return slug[:48].rstrip("-")


def change_id(slug: str, today: date) -> str:
    """`2026-10-10-item-archive`: sorts by date, and readable in a directory listing."""
    return f"{today.isoformat()}-{slugify(slug)}"


def namespace_for(cid: str) -> str:
    """A C# namespace unique to the change, so two changes' test classes never collide.

    Child of `Acceptance`, where AcceptanceBase lives, so the base class needs no using.
    """
    parts = re.split(r"[^A-Za-z0-9]+", cid)
    return "Acceptance.Change" + "".join(p[:1].upper() + p[1:] for p in parts if p)


def branch_for(cid: str) -> str:
    return f"change/{cid}"


def change_dir(repo: Path, cid: str) -> Path:
    return repo / CHANGES_DIR / cid


def _commit(repo: Path, message: str, cid: str) -> str:
    _run(["git", "add", "--", str(CHANGES_DIR / cid)], cwd=repo)
    _run(["git", "commit", "--quiet", "-m", message, "--trailer", f"{TRAILER}: {cid}"], cwd=repo)
    return _run(["git", "rev-parse", "HEAD"], cwd=repo)


def start_branch(repo: Path, cid: str, brief: str) -> str:
    """Branch off the checked-out default branch, commit the brief, push. Returns the sha."""
    branch = branch_for(cid)
    d = change_dir(repo, cid)
    if d.exists():
        raise ChangeError(f"{CHANGES_DIR / cid} already exists on the default branch; pick another id")
    _run(["git", "checkout", "--quiet", "-b", branch], cwd=repo)
    d.mkdir(parents=True)
    (d / "brief.md").write_text(brief.strip() + "\n", encoding="utf-8")
    sha = _commit(repo, f"change({cid}): the request", cid)
    _run(["git", "push", "--quiet", "-u", "origin", branch], cwd=repo)
    return sha


def add_tests(repo: Path, cid: str, files: list[Path]) -> str:
    """Commit the spec-tester's files into changes/<id>/ on the checked-out branch, push.

    The caller has put the checkout on the pull request's head (checkout_pr_branch).
    Returns the new head sha.
    """
    if not any(f.suffix == ".cs" for f in files):
        raise ChangeError("no acceptance test file to commit")
    d = change_dir(repo, cid)
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy(f, d / f.name)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    sha = _commit(repo, f"test({cid}): acceptance tests, written from the request by the spec-tester", cid)
    _run(["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo)
    return sha


def verify_untouched(repo: Path, cid: str, tests_sha: str, head_sha: str) -> list[str]:
    """Files under changes/<id>/ that differ between the harness's commit and the head."""
    _run(["git", "fetch", "--quiet", "origin", head_sha], cwd=repo)
    out = _run(["git", "diff", "--name-only", tests_sha, head_sha, "--", str(CHANGES_DIR)], cwd=repo)
    return [line for line in out.splitlines() if line]


def builder_note(cid: str) -> str:
    """Appended to the builder's briefing: the branch exists, and changes/ is not theirs."""
    return f"""
# This change

The harness has created branch `{branch_for(cid)}` and checked it out. The request is
committed on it as `{CHANGES_DIR / cid / 'brief.md'}`. Work on this branch and push to
it: section 3's `git checkout -b` does not apply, and do not create another branch.
Start the pull request body with the line `Change: {cid}`.

`changes/` is this application's record of changes: for each one, the request it was
built from and the acceptance tests written from that request by someone who never
sees the code. Read it if it helps. Do not write to it - the policy refuses, and CI
fails any commit of yours that touches it. After you push, the harness adds this
change's acceptance tests to the branch and CI runs them with the endpoint tests.
""".strip()


def fix_note(cid: str) -> str:
    """Appended to every fix-round brief of a change."""
    return f"""
## The acceptance tests

The tests in `{CHANGES_DIR / cid}/` were written from the request by someone who has
not seen the code. If one of them fails, the code does not do what the request says:
fix the code. Do not edit them (you cannot), and do not make them pass by recognising
their inputs. If you are certain a test contradicts the request, name the test, quote
the sentence of the request it contradicts, and stop without pushing.
""".strip()


def conform(app: Path, files: list[Path]) -> list[str]:
    """Bring a spec-tester's test files to the app's conventions, or say why they cannot be.

    CI checks CSharpier formatting and the Arrange/Act/Assert layout (AGENT.md,
    "Conventions") on the change record's files, and the builder cannot edit them -
    so a non-conforming file would be a red check nobody can fix. Formatting is
    mechanical, so the harness applies it; layout is the spec-tester's, so a
    violation is reported. Repositories without the conventions are left alone.
    """
    if not files:
        return []
    paths = [str(f) for f in files]
    manifest = app / "dotnet-tools.json"
    if manifest.is_file() and '"csharpier"' in manifest.read_text(encoding="utf-8"):
        _run(["dotnet", "tool", "restore"], cwd=app)
        _run(["dotnet", "csharpier", "format", *paths], cwd=app)
    checker = app / "scripts" / "check-test-layout.py"
    if not checker.is_file():
        return []
    result = subprocess.run(
        ["python3", str(checker), "--files", *paths], cwd=app, capture_output=True, text=True
    )
    if result.returncode == 0:
        return []
    return [line.split("::", 2)[-1] for line in result.stdout.splitlines() if line.startswith("::error")]


# What a change may touch and still be merged by the harness (step 2c, OQ-5).
# A migration stays with a human (OQ-7), and so does a dependency: nothing reviews
# a new package yet.
MERGEABLE_PREFIXES = ("src/", "tests/")
GATED = (
    ("src/App/Migrations/", "adds or changes a migration"),
    ("packages.lock.json", "changes a dependency"),
    ("Directory.Packages.props", "changes a dependency"),
)


def merge_blockers(repo: Path, cid: str, base: str, head_sha: str) -> list[str]:
    """Reasons the harness may not merge this change. Empty means it may."""
    _run(["git", "fetch", "--quiet", "origin", base, head_sha], cwd=repo)
    files = _run(["git", "diff", "--name-only", f"origin/{base}...{head_sha}"], cwd=repo).splitlines()
    blockers: list[str] = []
    for path in files:
        gated = next((why for pat, why in GATED if pat in path), None)
        if gated:
            blockers.append(f"{gated} ({path})")
        elif not (path.startswith(MERGEABLE_PREFIXES) or path.startswith(f"{CHANGES_DIR}/{cid}/")):
            blockers.append(f"touches {path}")
    return sorted(set(blockers))
