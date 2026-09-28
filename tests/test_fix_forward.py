"""
The fix-forward loop in harness/main.py, with GitHub and the agent faked out.

Tests the loop's decisions: when it starts a fix round, when it stops, and what
the run record says about why.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

from harness import checks as ck
from harness import main as m
from harness.report import RunRecord

PR = ck.PullRequest("main0034", "aaas-app-demo", 7)


class World:
    """GitHub as seen by the loop: a head sha per round, and checks per sha."""

    def __init__(self, heads: list[str], checks: dict[str, list[ck.CheckRun]]) -> None:
        self.heads = heads
        self.checks = checks
        self.pr_views = 0
        self.fix_briefs: list[str] = []

    def pr_head(self, pr: ck.PullRequest) -> dict:
        sha = self.heads[min(self.pr_views, len(self.heads) - 1)]
        self.pr_views += 1
        return {"headRefOid": sha, "headRefName": "feat/search", "baseRefName": "master"}


def red(name: str = "test") -> list[ck.CheckRun]:
    return [ck.CheckRun(1, name, "completed", "failure"), ck.CheckRun(2, "build", "completed", "success")]


GREEN_RUNS = [ck.CheckRun(3, "test", "completed", "success"), ck.CheckRun(4, "build", "completed", "success")]


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def setup(world: World, fix_rounds: int = 2, stopped_by: str | None = None):
        monkeypatch.setattr(ck, "pr_head", world.pr_head)
        monkeypatch.setattr(ck, "check_runs", lambda pr, sha: world.checks[sha])
        monkeypatch.setattr(ck, "job_log", lambda pr, c: f"##[error]{c.name} broke")
        real_wait = ck.wait_for_checks
        monkeypatch.setattr(
            ck, "wait_for_checks",
            lambda fetch, **kw: real_wait(fetch, sleep=lambda s: None, settle=0, **{k: v for k, v in kw.items() if k != "on_poll"}),
        )
        monkeypatch.setattr(m, "checkout_pr_branch", lambda repo, branch, sha: None)
        monkeypatch.setattr(m, "diff_stat", lambda repo, base: " Program.cs | 3 +++")

        async def fake_session(options, message, record, status, interactive):  # noqa: ANN001
            world.fix_briefs.append(message)
            outcome = m.SessionOutcome()
            outcome.stopped_by = stopped_by
            record.cost_usd += 0.25
            return outcome

        monkeypatch.setattr(m, "run_session", fake_session)

        args = argparse.Namespace(fix_rounds=fix_rounds, checks_timeout=600, request="search by title")
        record = RunRecord(run_id="t", directory=tmp_path / "run", task="create-app", request="x")
        record.rounds.append({"round": 0, "kind": "initial"})
        workspace = argparse.Namespace(app=tmp_path)
        code = asyncio.run(
            m.fix_forward(args, PR, workspace, "agent/create-app.md", lambda fix=False: None, record, m.StatusLine())
        )
        return code, record

    return setup


def test_green_on_first_push_starts_no_fix_round(harness) -> None:
    world = World(["aaa"], {"aaa": GREEN_RUNS})
    code, record = harness(world)
    assert code == 0
    assert world.fix_briefs == []
    assert record.end_reason == "checks green on first push"


def test_red_then_fixed_is_green_after_one_round(harness) -> None:
    world = World(["aaa", "bbb"], {"aaa": red(), "bbb": GREEN_RUNS})
    code, record = harness(world)
    assert code == 0
    assert len(world.fix_briefs) == 1
    assert "test broke" in world.fix_briefs[0]
    assert record.end_reason.startswith("checks green after 1 fix round ")
    assert [r["kind"] for r in record.rounds] == ["initial", "fix"]
    assert record.rounds[0]["failed_checks"] == ["test"]
    assert record.rounds[1]["checks"] == ck.GREEN
    assert (record.directory / "round-1-failure.log").read_text().startswith("== test")


def test_stops_after_the_allowed_rounds(harness) -> None:
    world = World(["aaa", "bbb", "ccc"], {"aaa": red(), "bbb": red(), "ccc": red()})
    code, record = harness(world, fix_rounds=2)
    assert code == 1
    assert len(world.fix_briefs) == 2
    assert "still red after 2 fix rounds" in record.end_reason


def test_a_round_that_pushes_nothing_ends_the_loop(harness) -> None:
    # The agent explained a blocker instead of pushing. Waiting again on the same
    # commit would re-read the same red and spend a second round on nothing.
    world = World(["aaa", "aaa"], {"aaa": red()})
    code, record = harness(world)
    assert code == 1
    assert len(world.fix_briefs) == 1
    assert record.end_reason == "fix round 1 ended without pushing a new commit"


def test_a_capped_fix_round_says_so(harness) -> None:
    world = World(["aaa", "bbb"], {"aaa": red(), "bbb": GREEN_RUNS})
    code, record = harness(world, stopped_by="error_max_turns")
    assert code == 1
    assert "error_max_turns" in record.end_reason


def test_skip_local_checks_reaches_only_the_initial_session() -> None:
    workspace = argparse.Namespace(app=Path("/w/aaas-app-demo"), deployments=Path("/w/d"), reference=Path("/w/r"))
    initial = m.app_briefing(workspace, "agent/create-app.md", fix=False, skip_local_checks=True)
    fix = m.app_briefing(workspace, "agent/create-app.md", fix=True, skip_local_checks=True)
    normal = m.app_briefing(workspace, "agent/create-app.md")
    assert "Skip section 4" in initial
    assert "Skip section 4" not in fix
    assert "Skip section 4" not in normal
    assert "This is a fix round" in fix
