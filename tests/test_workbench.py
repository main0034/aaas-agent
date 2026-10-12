"""
The workbench (harness/workbench.py): build, rebuild and ship as git, against a
local bare repository standing in for GitHub. The sessions are not run here; what
a builder would do is a file written between the harness's steps.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from harness import workbench as wb
from harness.policy import APP_PROTECTED_GLOBS, WORKBENCH_OFFLINE_REASON, ToolPolicy

REQUEST = """# Archive items

## What and why
Old items clutter the list.

## Rules
- An archived item is not listed.
"""
DAY1, DAY2 = date(2026, 10, 12), date(2026, 10, 13)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "-b", "master", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "--quiet", str(bare), str(seed), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        git("config", k, v, cwd=seed)
    (seed / "src").mkdir()
    (seed / "src/Program.cs").write_text("// app\n")
    (seed / "requests").mkdir()
    (seed / "requests/TEMPLATE.md").write_text("# Template\n")
    (seed / "requests/item-archive.md").write_text(REQUEST)
    git("add", ".", cwd=seed)
    git("commit", "--quiet", "-m", "init", cwd=seed)
    git("push", "--quiet", "origin", "master", cwd=seed)
    return bare


def clone(origin: Path, dest: Path) -> Path:
    git("clone", "--quiet", str(origin), str(dest), cwd=origin.parent)
    for k, v in (("user.name", "aaas-agent"), ("user.email", "a@a")):
        git("config", k, v, cwd=dest)
    return dest


def build(origin: Path, dest: Path, today: date = DAY1, code: str = "// archive\n", request: str | None = None,
          rebuild: bool = False) -> tuple[Path, wb.Branch, str]:
    repo = clone(origin, dest)
    branch = wb.find_branch(repo, "item-archive", today)
    start = wb.checkout(repo, branch, "master", "item-archive", request)
    (repo / "src/Archive.cs").write_text(code)  # the builder's work
    sha, changed = wb.commit_build(repo, "item-archive", start, rebuild)
    assert "src/Archive.cs" in changed
    wb.push(repo, branch)
    return repo, branch, sha


def test_first_build_creates_a_dated_branch_and_records_the_request(origin: Path, tmp_path: Path) -> None:
    repo, branch, sha = build(origin, tmp_path / "a")
    assert branch.name == "change/2026-10-12-item-archive"
    assert branch.cid == "2026-10-12-item-archive"
    built = wb.last_build(repo, "item-archive")
    assert built == (sha, wb.request_blob(repo, "item-archive"))
    assert wb.read_blob(repo, built[1]) == REQUEST
    assert "change/2026-10-12-item-archive" in git("ls-remote", "--heads", str(origin), cwd=tmp_path)


def test_the_branch_is_found_on_a_later_day(origin: Path, tmp_path: Path) -> None:
    build(origin, tmp_path / "a")
    repo = clone(origin, tmp_path / "b")
    branch = wb.find_branch(repo, "item-archive", DAY2)
    assert branch.exists and branch.cid == "2026-10-12-item-archive"


def test_two_branches_for_one_name_is_refused(origin: Path, tmp_path: Path) -> None:
    build(origin, tmp_path / "a")
    repo = clone(origin, tmp_path / "b")
    git("push", "--quiet", "origin", "master:refs/heads/change/2026-10-13-item-archive", cwd=repo)
    with pytest.raises(wb.WorkbenchError, match="more than one"):
        wb.find_branch(repo, "item-archive", DAY2)


def test_a_similar_name_is_not_the_same_change(origin: Path, tmp_path: Path) -> None:
    build(origin, tmp_path / "a")
    repo = clone(origin, tmp_path / "b")
    assert not wb.find_branch(repo, "archive", DAY2).exists


def test_a_request_from_the_chat_is_committed_on_the_new_branch(origin: Path, tmp_path: Path) -> None:
    repo = clone(origin, tmp_path / "a")
    branch = wb.find_branch(repo, "item-colour", DAY1)
    wb.checkout(repo, branch, "master", "item-colour", "# Colour items\n")
    assert (repo / "requests/item-colour.md").read_text() == "# Colour items\n"
    assert git("log", "-1", "--format=%s", cwd=repo) == "request(item-colour): Colour items"


def test_no_request_is_refused_with_the_pending_ones(origin: Path, tmp_path: Path) -> None:
    repo = clone(origin, tmp_path / "a")
    branch = wb.find_branch(repo, "item-colour", DAY1)
    with pytest.raises(wb.WorkbenchError, match="Pending: item-archive"):
        wb.checkout(repo, branch, "master", "item-colour", None)


def test_the_builder_may_not_touch_the_request(origin: Path, tmp_path: Path) -> None:
    repo = clone(origin, tmp_path / "a")
    branch = wb.find_branch(repo, "item-archive", DAY1)
    start = wb.checkout(repo, branch, "master", "item-archive", None)
    (repo / "src/Archive.cs").write_text("// x\n")
    (repo / "requests/item-archive.md").write_text(REQUEST + "- and a sneaky rule\n")
    with pytest.raises(wb.WorkbenchError, match="requests/item-archive.md"):
        wb.commit_build(repo, "item-archive", start, False)


def test_rebuild_sees_the_old_request_and_ship_waits_for_it(origin: Path, tmp_path: Path) -> None:
    build(origin, tmp_path / "a")
    # The operator edits the request on the branch and pushes.
    mine = clone(origin, tmp_path / "mine")
    git("checkout", "--quiet", "change/2026-10-12-item-archive", cwd=mine)
    edited = REQUEST + "- An archived item can be restored.\n"
    (mine / "requests/item-archive.md").write_text(edited)
    git("commit", "--quiet", "-am", "request: restore", cwd=mine)
    git("push", "--quiet", cwd=mine)

    repo = clone(origin, tmp_path / "b")
    branch = wb.find_branch(repo, "item-archive", DAY2)
    wb.checkout(repo, branch, "master", "item-archive", None)
    built = wb.last_build(repo, "item-archive")
    assert built and wb.read_blob(repo, built[1]) == REQUEST
    assert wb.ship_ready(repo, "item-archive") == [
        f"requests/item-archive.md changed after the last build ({built[0][:7]}): rebuild, preview, then ship"
    ]
    message = wb.rebuild_message("item-archive", edited, REQUEST, "src/Archive.cs | 1 +", "master")
    assert "+- An archived item can be restored." in message

    _, _, sha2 = build(origin, tmp_path / "c", today=DAY2, code="// archive and restore\n", rebuild=True)
    repo = clone(origin, tmp_path / "d")
    wb.checkout(repo, wb.find_branch(repo, "item-archive", DAY2), "master", "item-archive", None)
    assert wb.last_build(repo, "item-archive")[0] == sha2
    assert wb.ship_ready(repo, "item-archive") == []
    assert wb.build_count(repo, "item-archive") == 2
    assert "request: restore" in wb.history(repo, "item-archive", "master")


def test_ship_before_any_build_is_refused(origin: Path, tmp_path: Path) -> None:
    repo = clone(origin, tmp_path / "a")
    git("checkout", "--quiet", "-b", "change/2026-10-12-item-archive", cwd=repo)
    assert "build first" in wb.ship_ready(repo, "item-archive")[0]


def test_the_record_moves_the_request_and_adds_the_tests_in_one_harness_commit(origin: Path, tmp_path: Path) -> None:
    repo, branch, _ = build(origin, tmp_path / "a")
    tests = tmp_path / "held"
    tests.mkdir()
    (tests / "A-ArchiveAcceptance.cs").write_text("// A\n")
    (tests / "A-NOTES.md").write_text("notes\n")
    sha = wb.record(repo, branch, "item-archive", sorted(tests.iterdir()))
    assert not (repo / "requests/item-archive.md").exists()
    rec = repo / "changes/2026-10-12-item-archive"
    assert (rec / "brief.md").read_text() == REQUEST
    assert sorted(p.name for p in rec.iterdir()) == ["A-ArchiveAcceptance.cs", "A-NOTES.md", "brief.md"]
    trailer = git("log", "-1", "--format=%(trailers:key=AaaS-Change,valueonly)", sha, cwd=repo)
    assert trailer == "2026-10-12-item-archive"
    touched = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha, "--", "changes", cwd=repo).splitlines()
    assert {p.split("/")[1] for p in touched} == {"2026-10-12-item-archive"}


def test_the_record_needs_a_test_file(origin: Path, tmp_path: Path) -> None:
    repo, branch, _ = build(origin, tmp_path / "a")
    notes = tmp_path / "NOTES.md"
    notes.write_text("x")
    with pytest.raises(wb.WorkbenchError, match="no acceptance test"):
        wb.record(repo, branch, "item-archive", [notes])


@pytest.mark.parametrize(
    "command",
    ["gh pr create --title x --body-file /tmp/b.md", "git push -u origin HEAD", "git fetch origin", "gh pr view 3"],
)
def test_a_workbench_builder_cannot_push_or_open_a_pr(command: str) -> None:
    app = Path("/work/runs/x/workspace/aaas-app-demo")
    policy = ToolPolicy(
        writable_roots=[app, Path("/tmp")], protected_globs=APP_PROTECTED_GLOBS, offline=True,
        offline_reason=WORKBENCH_OFFLINE_REASON, offline_git_reason=WORKBENCH_OFFLINE_REASON,
    )
    decision = policy.check("Bash", {"command": command})
    assert not decision.allow and "workbench build" in decision.reason
    assert policy.check("Bash", {"command": "git commit -m x"}).allow
    assert not policy.check("Write", {"file_path": str(app / "preview.yml")}).allow
    assert not policy.check("Write", {"file_path": str(app / ".run/Preview.run.xml")}).allow


# -- build, rebuild, ship end to end, with fake sessions ---------------------------

import argparse  # noqa: E402
import asyncio  # noqa: E402

from harness import checks as ck  # noqa: E402
from harness import main as m  # noqa: E402
from harness import workbench_run as wr  # noqa: E402
from harness import workspace as ws  # noqa: E402

FULL = """# Archive items

## What and why
Old items clutter the list.

## Rules
- An archived item is not listed by `GET /items`.

## Interface
- `POST /items/{id}/archive` - 204, or 404 for no such item.
"""


def test_build_rebuild_ship(origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployments = tmp_path / "deployments"
    (deployments / "agent").mkdir(parents=True)
    for f in ("PROMPT.md", "create-app.md", "write-acceptance.md"):
        (deployments / "agent" / f).write_text(f"# {f}\n")
    reference = tmp_path / "reference"
    reference.mkdir()
    mine = clone(origin, tmp_path / "mine")
    (mine / "requests/item-archive.md").write_text(FULL)
    (mine / m.ACCEPTANCE_DIR).mkdir(parents=True)
    (mine / m.ACCEPTANCE_BASE).write_text("// base\n")
    git("add", ".", cwd=mine)
    git("commit", "--quiet", "-m", "request", cwd=mine)
    git("push", "--quiet", "origin", "master", cwd=mine)

    def fake_prepare(root: Path, owner: str, app_repo: str | None = None, **_: object) -> ws.Workspace:
        root.mkdir(parents=True, exist_ok=True)
        return ws.Workspace(root=root, deployments=deployments, reference=reference, app=clone(origin, root / app_repo))

    def fake_isolated(dest: Path, owner: str, app_repo: str) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        app = clone(origin, dest)
        ws.isolate(app)
        return app

    spec_tmp = Path(m.tempfile.mkdtemp(prefix="aaas-test-", dir=Path.home()))
    monkeypatch.setattr(m, "spec_tmp_dir", lambda: str(spec_tmp))
    monkeypatch.setattr(ws, "prepare", fake_prepare)
    monkeypatch.setattr(ws, "clone_isolated", fake_isolated)
    monkeypatch.setattr(ws, "head_sha", lambda repo: "0" * 40)
    seen: dict[str, list] = {"builder": [], "spec": []}

    async def fake_session(options, message, record, status, interactive):  # noqa: ANN001
        cwd = Path(options.cwd)
        policy = options.hooks["PreToolUse"][0].hooks[0]
        if "CLAUDE_CONFIG_DIR" in options.env:  # a spec-tester, on master
            seen["spec"].append((message, (cwd / "src/Archive.cs").exists()))
            (cwd / m.ACCEPTANCE_DIR / "ArchiveAcceptance.cs").write_text("// spec\n")
            return m.SessionOutcome()
        push = await policy({"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}}, None, None)
        seen["builder"].append((message, push["hookSpecificOutput"]["permissionDecision"],
                                "workbench build" in options.system_prompt["append"]))
        (cwd / "src/Archive.cs").write_text(f"// build {len(seen['builder'])}\n")
        out = m.SessionOutcome()
        out.last_text = "Built it on the Items page."
        return out

    monkeypatch.setattr(m, "run_session", fake_session)
    monkeypatch.setattr(wr, "run_session", fake_session)

    def args(task: str, **kw: object) -> argparse.Namespace:
        base = dict(task=task, request=None, app_repo="app", owner="o", runs_dir=str(tmp_path / "runs"),
                    run_id=f"r-{task}", change="item-archive", model=None, max_turns=5, fix_rounds=0,
                    checks_timeout=60, spec_testers=1, auto_merge=False, non_interactive=True)
        base.update(kw)
        return argparse.Namespace(**base)

    today = m.datetime.now(m.timezone.utc).date()
    cid = f"{today.isoformat()}-item-archive"
    branch = f"change/{cid}"

    # build
    assert asyncio.run(wr.run_build(args("build"))) == 0
    assert seen["builder"][0] == (FULL, "deny", True)
    assert git("show", f"{branch}:src/Archive.cs", cwd=origin) == "// build 1"

    # rebuild with the request unchanged is refused, and spends nothing
    assert asyncio.run(wr.run_build(args("rebuild", run_id="r-same"))) == 2
    assert len(seen["builder"]) == 1

    # ship before the rebuild of an edited request is refused
    git("fetch", "--quiet", cwd=mine)
    git("checkout", "--quiet", branch, cwd=mine)
    edited = FULL.replace("## Interface", "- An archived item can be restored.\n\n## Interface")
    (mine / "requests/item-archive.md").write_text(edited)
    git("commit", "--quiet", "-am", "request: restore", cwd=mine)
    git("push", "--quiet", cwd=mine)
    assert asyncio.run(wr.run_ship(args("ship", run_id="r-early"))) == 2

    # rebuild
    assert asyncio.run(wr.run_build(args("rebuild"))) == 0
    message = seen["builder"][1][0]
    assert message.startswith("# The request has changed") and "+- An archived item can be restored." in message

    # ship: the spec-tester gets the final request and master; the PR is the harness's
    created: list[list[str]] = []

    def fake_gh(a: list[str]) -> str:
        created.append(a)
        return "https://github.com/o/app/pull/7\n"

    monkeypatch.setattr(ck, "gh", fake_gh)
    monkeypatch.setattr(ck, "pr_head", lambda p: {
        "headRefOid": git("rev-parse", branch, cwd=origin), "headRefName": branch, "baseRefName": "master"})
    monkeypatch.setattr(ck, "check_runs", lambda p, sha: [
        ck.CheckRun(1, "test", "completed", "success"), ck.CheckRun(2, "build", "completed", "success")])
    merges: list[str] = []
    monkeypatch.setattr(ck, "merge", lambda p, sha: merges.append(sha))
    real_wait = ck.wait_for_checks
    monkeypatch.setattr(ck, "wait_for_checks", lambda fetch, **kw: real_wait(
        fetch, sleep=lambda s: None, settle=0, **{k: v for k, v in kw.items() if k != "on_poll"}))

    assert asyncio.run(wr.run_ship(args("ship", auto_merge=True))) == 0
    assert seen["spec"] == [(edited, False)]
    tree = git("ls-tree", "-r", "--name-only", branch, cwd=origin).splitlines()
    assert f"changes/{cid}/brief.md" in tree and f"changes/{cid}/ArchiveAcceptance.cs" in tree
    assert "requests/item-archive.md" not in tree
    assert git("show", f"{branch}:changes/{cid}/brief.md", cwd=origin) == edited.strip()
    assert created[0][:2] == ["pr", "create"] and "--head" in created[0]
    assert merges == [git("rev-parse", branch, cwd=origin)]
    spec_tmp.rmdir()
