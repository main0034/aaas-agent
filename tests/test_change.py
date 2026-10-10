"""
create-change (harness/change.py and run_change in main.py), against a local bare
repository standing in for GitHub and fake agent sessions.

The test that matters most is `test_builder_cannot_find_the_tests`: while the
builder runs, nothing the spec-tester wrote may exist on disk.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from datetime import date
from pathlib import Path

import pytest

from harness import change as chg
from harness import checks as ck
from harness import main as m
from harness import workspace as ws

MARKER = "SPEC_TESTER_WAS_HERE"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    """A bare 'GitHub' with one commit on master."""
    bare = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "-b", "master", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "--quiet", str(bare), str(seed), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        git("config", k, v, cwd=seed)
    (seed / "README.md").write_text("app\n")
    git("add", ".", cwd=seed)
    git("commit", "--quiet", "-m", "init", cwd=seed)
    git("push", "--quiet", "origin", "master", cwd=seed)
    return bare


def clone(origin: Path, dest: Path) -> Path:
    git("clone", "--quiet", str(origin), str(dest), cwd=dest.parent if dest.parent.exists() else Path("/"))
    for k, v in (("user.name", "aaas-agent"), ("user.email", "a@a")):
        git("config", k, v, cwd=dest)
    return dest


# -- change.py ----------------------------------------------------------------


def test_ids_and_namespaces() -> None:
    cid = chg.change_id("Item archive!", date(2026, 10, 10))
    assert cid == "2026-10-10-item-archive"
    assert chg.branch_for(cid) == "change/2026-10-10-item-archive"
    assert chg.namespace_for(cid) == "Acceptance.Change20261010ItemArchive"


def test_brief_then_tests_each_carry_the_trailer(origin: Path, tmp_path: Path) -> None:
    repo = clone(origin, tmp_path / "app")
    cid = "2026-10-10-x"
    chg.start_branch(repo, cid, "Do the thing.\n")
    (tmp_path / "XAcceptance.cs").write_text("// t\n")
    (tmp_path / "NOTES.md").write_text("notes\n")
    sha = chg.add_tests(repo, cid, [tmp_path / "XAcceptance.cs", tmp_path / "NOTES.md"])

    on_origin = git("ls-tree", "-r", "--name-only", f"change/{cid}", cwd=origin).splitlines()
    assert sorted(f for f in on_origin if f.startswith("changes/")) == [
        f"changes/{cid}/NOTES.md", f"changes/{cid}/XAcceptance.cs", f"changes/{cid}/brief.md",
    ]
    trailers = git("log", "--format=%(trailers:key=AaaS-Change,valueonly)", f"master..{sha}", cwd=repo)
    assert trailers.split() == [cid, cid]
    assert chg.verify_untouched(repo, cid, sha, sha) == []


def test_an_edit_after_the_harness_commit_is_found(origin: Path, tmp_path: Path) -> None:
    repo = clone(origin, tmp_path / "app")
    cid = "2026-10-10-x"
    chg.start_branch(repo, cid, "brief")
    (tmp_path / "XAcceptance.cs").write_text("Assert.Equal(1, x);\n")
    tests_sha = chg.add_tests(repo, cid, [tmp_path / "XAcceptance.cs"])
    (repo / "changes" / cid / "XAcceptance.cs").write_text("Assert.Equal(2, x);\n")
    git("commit", "--quiet", "-am", "fix: make it pass", cwd=repo)
    git("push", "--quiet", "origin", f"change/{cid}", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    assert chg.verify_untouched(repo, cid, tests_sha, head) == [f"changes/{cid}/XAcceptance.cs"]


def test_tests_are_required(origin: Path, tmp_path: Path) -> None:
    repo = clone(origin, tmp_path / "app")
    chg.start_branch(repo, "2026-10-10-x", "brief")
    (tmp_path / "NOTES.md").write_text("n")
    with pytest.raises(chg.ChangeError):
        chg.add_tests(repo, "2026-10-10-x", [tmp_path / "NOTES.md"])


def test_an_existing_change_id_is_refused(origin: Path, tmp_path: Path) -> None:
    repo = clone(origin, tmp_path / "app")
    (repo / "changes" / "2026-10-10-x").mkdir(parents=True)
    with pytest.raises(chg.ChangeError):
        chg.start_branch(repo, "2026-10-10-x", "brief")


def test_the_builder_may_not_write_changes(tmp_path: Path) -> None:
    policy = m.ToolPolicy(writable_roots=[tmp_path], protected_globs=m.APP_PROTECTED_GLOBS)
    assert not policy.check("Write", {"file_path": str(tmp_path / "changes/2026-10-10-x/A.cs")}).allow
    assert policy.check("Write", {"file_path": str(tmp_path / "src/App/Program.cs")}).allow


# -- run_change, end to end with fakes ----------------------------------------


def test_builder_cannot_find_the_tests(origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "runs"
    deployments = tmp_path / "deployments"
    (deployments / "agent").mkdir(parents=True)
    for f in ("PROMPT.md", "create-app.md", "write-acceptance.md"):
        (deployments / "agent" / f).write_text(f"# {f}\n")
    reference = tmp_path / "reference"
    reference.mkdir()

    def fake_prepare(root: Path, owner: str, app_repo: str | None = None, **_: object) -> ws.Workspace:
        root.mkdir(parents=True, exist_ok=True)
        return ws.Workspace(root=root, deployments=deployments, reference=reference, app=clone(origin, root / app_repo))

    def fake_isolated(dest: Path, owner: str, app_repo: str) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        app = clone(origin, dest)
        ws.isolate(app)
        return app

    (tmp_path / "tmpdir").mkdir()
    monkeypatch.setattr(m.tempfile, "tempdir", str(tmp_path / "tmpdir"))
    monkeypatch.setattr(ws, "prepare", fake_prepare)
    monkeypatch.setattr(ws, "clone_isolated", fake_isolated)
    monkeypatch.setattr(ws, "head_sha", lambda repo: "0" * 40)
    pr = ck.PullRequest("main0034", "app", 9)
    seen: dict[str, object] = {}

    async def fake_session(options, message, record, status, interactive):  # noqa: ANN001
        cwd = Path(options.cwd)
        if options.env.get("GH_TOKEN") == "":  # the spec-tester
            seen["spec_config"] = options.env["CLAUDE_CONFIG_DIR"]
            (cwd / m.ACCEPTANCE_DIR / "ThingAcceptance.cs").write_text(f"// {MARKER}\n")
            (cwd / m.ACCEPTANCE_DIR / "NOTES.md").write_text("notes\n")
            record.append_message({"tool": "Write", "content": MARKER})
            record.cost_usd += 0.5
            return m.SessionOutcome()
        if "Fix round" in message:
            raise AssertionError("no fix round expected")
        # The builder: look everywhere a curious session might.
        hits = subprocess.run(["grep", "-rl", MARKER, str(tmp_path)], capture_output=True, text=True).stdout
        seen["hits"] = hits.split()
        seen["spec_config_exists"] = Path(seen["spec_config"]).exists()
        seen["builder_note"] = "change/" in options.system_prompt["append"]
        (cwd / "code.cs").write_text("code\n")
        git("add", ".", cwd=cwd)
        git("commit", "--quiet", "-m", "feat", cwd=cwd)
        git("push", "--quiet", "origin", "HEAD", cwd=cwd)
        record.note_text(pr.url)
        record.cost_usd += 1.0
        return m.SessionOutcome()

    monkeypatch.setattr(m, "run_session", fake_session)

    def pr_head(_pr: ck.PullRequest) -> dict:
        sha = git("rev-parse", f"change/{seen['cid']}", cwd=origin)
        return {"headRefOid": sha, "headRefName": f"change/{seen['cid']}", "baseRefName": "master"}

    monkeypatch.setattr(ck, "pr_head", pr_head)
    monkeypatch.setattr(ck, "check_runs", lambda p, sha: [ck.CheckRun(1, "test", "completed", "success"),
                                                        ck.CheckRun(2, "build", "completed", "success")])
    real_wait = ck.wait_for_checks
    monkeypatch.setattr(ck, "wait_for_checks", lambda fetch, **kw: real_wait(
        fetch, sleep=lambda s: None, settle=0, **{k: v for k, v in kw.items() if k != "on_poll"}))

    seen["cid"] = chg.change_id("thing", m.datetime.now(m.timezone.utc).date())
    args = argparse.Namespace(
        task="create-change", request="Add a thing.", app_repo="app", owner="o", runs_dir=str(runs),
        run_id="r1", change="thing", model=None, max_turns=5, fix_rounds=1, checks_timeout=60,
        skip_local_checks=False, non_interactive=True,
    )
    code = asyncio.run(m.run_change(args))

    assert seen["hits"] == [], f"the builder could read the spec-tester's output: {seen['hits']}"
    assert seen["spec_config_exists"] is False
    assert seen["builder_note"]
    assert code == 0
    report = (runs / "r1" / "report.md").read_text()
    assert "acceptance tests unchanged" in report
    assert (runs / "r1" / "spec" / "transcript.jsonl").read_text().count(MARKER) == 1
    on_branch = git("ls-tree", "-r", "--name-only", f"change/{seen['cid']}", cwd=origin)
    assert f"changes/{seen['cid']}/ThingAcceptance.cs" in on_branch
    assert f"changes/{seen['cid']}/brief.md" in on_branch
    assert not os.path.exists(runs / "r1" / "workspace" / "app" / m.ACCEPTANCE_DIR / "ThingAcceptance.cs")
