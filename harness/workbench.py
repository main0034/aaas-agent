"""
The workbench: build a change on a branch, preview it, change the request, ship it.

Roadmap step 5a. `create-change` freezes the request at the start and ships in
one go. The workbench splits that apart so the operator can see a change before
anyone tests it independently, and change his mind about what he asked for:

  build    the request (`requests/<name>.md`) gets a branch, `change/<date>-<name>`,
           and a builder writes the code on it. No pull request, no spec-tester.
  rebuild  the operator has edited the request on the branch. A fresh builder gets
           the new request, what changed in it, and the branch as it stands.
  ship     the request is final. Two spec-testers write hidden tests from it on
           `master`, the harness moves the request into `changes/<id>/` with the
           tests, opens the pull request, and from there it is `create-change`:
           checks, fix rounds, the D-26 merge.

What keeps the spec-testers independent is unchanged: they get `master` only,
with no remote, token or `gh` (workspace.isolate). What changed is the order:
the code exists before the tests do, so "written before any code existed"
becomes "written by someone never shown the code" (D-25, amended 12 October).

The builder never pushes in a workbench run. Its session has no `gh` and no git
network (the offline policy); the harness commits what it left and pushes. Each
build commit carries `AaaS-Build: <name>` and `AaaS-Request: <blob>`, the git
object id of the request it was built from. That is how `rebuild` knows what
the request said last time, and how `ship` knows the code was built from the
request it is about to test.

Everything in this module is git, so it is tested against real repositories in
tests/test_workbench.py. The sessions are run from main.py.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import change as chg
from .workspace import _run

BUILD_TRAILER = "AaaS-Build"
REQUEST_TRAILER = "AaaS-Request"
BRANCH_PREFIX = "change/"
_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}-(?P<slug>.+)$")


class WorkbenchError(RuntimeError):
    pass


@dataclass
class Branch:
    name: str  # change/2026-10-12-item-archive
    cid: str  # 2026-10-12-item-archive: the change id, and the record's directory at ship
    exists: bool  # on origin


def request_path(name: str) -> Path:
    return chg.REQUESTS_DIR / f"{name}.md"


def find_branch(repo: Path, name: str, today: date) -> Branch:
    """The workbench branch for `name` on origin, or the one a build would create today.

    Matched by the part after the date, so a change started yesterday is found
    today. Two branches for one name is an operator's mistake, refused rather
    than guessed.
    """
    slug = chg.slugify(name)
    out = _run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH_PREFIX}*"], cwd=repo)
    found = []
    for line in out.splitlines():
        ref = line.split("\t", 1)[-1].removeprefix("refs/heads/")
        m = _DATED.match(ref.removeprefix(BRANCH_PREFIX))
        if m and m.group("slug") == slug:
            found.append(ref)
    if len(found) > 1:
        raise WorkbenchError(f"more than one branch for {name!r}: {', '.join(sorted(found))}. Delete the stale one.")
    if found:
        cid = found[0].removeprefix(BRANCH_PREFIX)
        return Branch(name=found[0], cid=cid, exists=True)
    cid = chg.change_id(slug, today)
    return Branch(name=chg.branch_for(cid), cid=cid, exists=False)


def checkout(repo: Path, branch: Branch, base: str, name: str, request_text: str | None) -> str:
    """Put the checkout on the change branch. Returns the sha the session starts from.

    A new branch starts from `base`. Its request is `request_text` when given (the
    chat's "Try it", or `--request`), committed by the harness; otherwise it must
    already be `requests/<name>.md` on `base`, committed by the operator.
    """
    rel = request_path(name)
    if branch.exists:
        if request_text is not None:
            raise WorkbenchError(
                f"{branch.name} already exists; its request is {rel} on that branch. "
                "Edit it there and rebuild, rather than passing a new one."
            )
        _run(["git", "fetch", "--quiet", "origin", branch.name], cwd=repo)
        _run(["git", "checkout", "--quiet", "-B", branch.name, f"origin/{branch.name}"], cwd=repo)
    else:
        _run(["git", "checkout", "--quiet", "-b", branch.name], cwd=repo)
        if request_text is not None:
            if (repo / rel).exists():
                raise WorkbenchError(f"{rel} is already on {base}; build from it without --request")
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(request_text.strip() + "\n", encoding="utf-8")
            _run(["git", "add", "--", str(rel)], cwd=repo)
            _run(["git", "commit", "--quiet", "-m", f"request({name}): {title(request_text) or name}"], cwd=repo)
    if not (repo / rel).is_file():
        pending = sorted(p.stem for p in (repo / chg.REQUESTS_DIR).glob("*.md") if p.stem != "TEMPLATE")
        raise WorkbenchError(f"no {rel} on {branch.name if branch.exists else base}. Pending: {', '.join(pending) or '(none)'}")
    return head(repo)


def head(repo: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], cwd=repo)


def request_blob(repo: Path, name: str, ref: str = "HEAD") -> str:
    return _run(["git", "rev-parse", f"{ref}:{request_path(name).as_posix()}"], cwd=repo)


def read_blob(repo: Path, blob: str) -> str:
    return _run(["git", "cat-file", "-p", blob], cwd=repo) + "\n"


def last_build(repo: Path, name: str) -> tuple[str, str] | None:
    """(commit, request blob) of the newest build on the checked-out branch, or None."""
    out = _run(
        ["git", "log", "--format=%H %(trailers:key=" + REQUEST_TRAILER + ",valueonly,separator=)",
         "--grep", f"^{BUILD_TRAILER}: {re.escape(name)}$"],
        cwd=repo,
    )
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            return parts[0], parts[1]
    return None


def request_diff(old: str, new: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"{name}.md (last build)", tofile=f"{name}.md (now)",
        )
    )


def title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def guarded_paths(repo: Path, start: str, name: str) -> list[str]:
    """Files the builder may not change, changed since `start`: the request and the record.

    The policy refuses these writes already. This is the same rule checked on the
    result, so a refusal the policy missed still stops the push.
    """
    paths = ["--", str(chg.REQUESTS_DIR), str(chg.CHANGES_DIR)]
    # Committed since start, or changed in the working tree, or new and untracked.
    out = _run(["git", "diff", "--name-only", start, *paths], cwd=repo)
    new = _run(["git", "ls-files", "--others", "--exclude-standard", *paths], cwd=repo)
    return sorted({*out.splitlines(), *new.splitlines()} - {""})


def commit_build(repo: Path, name: str, start: str, rebuild: bool) -> tuple[str, list[str]]:
    """Commit what the builder left, with the build trailers. Returns (sha, files changed since start).

    The trailer commit is made even when the builder committed its own work, so
    every build is findable by `last_build`; it is then empty.
    """
    bad = guarded_paths(repo, start, name)
    if bad:
        raise WorkbenchError(f"the builder changed {', '.join(bad)}; nothing was pushed")
    _run(["git", "add", "-A"], cwd=repo)
    blob = request_blob(repo, name)
    verb = "rebuilt" if rebuild else "built"
    _run(
        ["git", "commit", "--quiet", "--allow-empty", "-m", f"build({name}): {verb} from the request",
         "--trailer", f"{BUILD_TRAILER}: {name}", "--trailer", f"{REQUEST_TRAILER}: {blob}"],
        cwd=repo,
    )
    changed = _run(["git", "diff", "--name-only", start, "HEAD"], cwd=repo).splitlines()
    return head(repo), changed


def push(repo: Path, branch: Branch) -> None:
    _run(["git", "push", "--quiet", "-u", "origin", f"HEAD:refs/heads/{branch.name}"], cwd=repo)


def ship_ready(repo: Path, name: str) -> list[str]:
    """Why the checked-out branch cannot ship yet. Empty means it can."""
    built = last_build(repo, name)
    if built is None:
        return [f"nothing has been built from {request_path(name)} yet: build first"]
    if built[1] != request_blob(repo, name):
        return [f"{request_path(name)} changed after the last build ({built[0][:7]}): rebuild, preview, then ship"]
    return []


def record(repo: Path, branch: Branch, name: str, files: list[Path]) -> str:
    """Move the request into changes/<id>/ with the spec-testers' tests, in one harness commit. Returns the sha.

    One commit carrying `AaaS-Change: <id>` touches the record, which is what the
    app's scripts/check-changes.sh requires.
    """
    if not any(f.suffix == ".cs" for f in files):
        raise WorkbenchError("no acceptance test file to commit")
    d = chg.change_dir(repo, branch.cid)
    if d.exists():
        raise WorkbenchError(f"{chg.CHANGES_DIR / branch.cid} already exists")
    d.mkdir(parents=True)
    _run(["git", "mv", str(request_path(name)), str(chg.CHANGES_DIR / branch.cid / "brief.md")], cwd=repo)
    for f in files:
        (d / f.name).write_bytes(f.read_bytes())
    return chg._commit(  # noqa: SLF001 - the same commit shape create-change makes
        repo,
        f"change({branch.cid}): the request and its acceptance tests, written by the spec-testers",
        branch.cid,
    )


def history(repo: Path, name: str, base: str) -> str:
    """How the request moved on the branch: one line per commit that changed it."""
    return _run(
        ["git", "log", "--reverse", "--format=- %h %s", f"origin/{base}..HEAD", "--", str(request_path(name))],
        cwd=repo,
    )


# -- what the builder is told ------------------------------------------------


def build_note(branch: Branch, name: str) -> str:
    """Appended to the builder's briefing in a workbench run. Overrides runbook section 5."""
    return f"""
# This run: a workbench build

The operator will try this change on his own machine before anyone else sees it.
The harness has checked out branch `{branch.name}`. The request is
`{request_path(name)}` on it.

- Do sections 1 to 4 of the runbook: read, understand, write, prove it.
- **Skip section 5.** Do not push and do not open a pull request. There is no
  `gh` and no git network in this run, on purpose. You may commit locally; when
  you stop, the harness commits whatever you left and pushes the branch.
- Do not write to `{chg.REQUESTS_DIR}/` or `{chg.CHANGES_DIR}/`. The request is the operator's;
  if it is wrong or contradicts itself, say so and stop.
- Nobody can answer a question during this run. Where the request is silent,
  make the smallest reasonable choice and say what you chose in your last message.
- The change will be tested later, from the request, by someone who never sees
  your code. Build exactly what it says, including the *Screen* section.

Stop when section 4's commands pass, and say in a few sentences what you built,
on which page, and what you chose where the request was silent.
""".strip()


def rebuild_message(name: str, new: str, old: str, diff_stat: str, base: str) -> str:
    """The first message of a rebuild session: the request now, and how it moved."""
    return f"""
# The request has changed

This branch already has an implementation of an earlier version of this request,
built by someone else. The operator tried it and changed the request. Change the
code so that it does what the request says **now**: keep what still fits, change
what the edit changed, and remove what the request no longer asks for (but never
an applied migration - see the runbook's section 3).

## The request now (`{request_path(name)}`)

{new.strip()}

## What changed in it since the last build

```diff
{request_diff(old, new, name).strip()}
```

## The branch so far, against `{base}`

```
{diff_stat.strip() or '(no changes)'}
```

`git diff origin/{base}...HEAD` shows the earlier work in full, and `git log`
its commits.
""".strip()


def pr_body(branch: Branch, name: str, request: str, hist: str, builds: int) -> str:
    return f"""Change: {branch.cid}

{title(request) or name}

Built on the workbench: the operator previewed it and settled the request before it shipped.
{builds} build{'s' if builds != 1 else ''} on `{branch.name}`. How the request moved:

{hist.strip() or '- (unchanged since the first build)'}

The request and the acceptance tests written from it, by two spec-testers who saw `master` only,
are in `changes/{branch.cid}/`. Opened by the harness, not the builder.
"""


def build_count(repo: Path, name: str) -> int:
    out = _run(["git", "log", "--format=%H", "--grep", f"^{BUILD_TRAILER}: {re.escape(name)}$"], cwd=repo)
    return len(out.splitlines())
