"""
Tests for the fix-forward decisions in harness/checks.py.

What is tested is what decides something: whether a commit is green, red or
still running; what part of a log the next session sees; what the next session
is told. Talking to GitHub is plumbing and fails loudly on its first run.
"""

from __future__ import annotations

import pytest

from harness.checks import (
    GREEN,
    PENDING,
    RED,
    CheckRun,
    ChecksError,
    fix_brief,
    latest_per_name,
    parse_pr_url,
    trim_log,
    verdict,
    wait_for_checks,
)


def run(name: str, status: str = "completed", conclusion: str | None = "success", id: int = 1) -> CheckRun:
    return CheckRun(id=id, name=name, status=status, conclusion=conclusion)


# -- verdict -------------------------------------------------------------------


def test_no_checks_yet_is_pending_not_green() -> None:
    # The state immediately after a push. Reading it as green would merge untested code.
    v = verdict([])
    assert v.state == PENDING
    assert v.waiting_for == ["test", "build"]


def test_all_required_passed_is_green() -> None:
    assert verdict([run("test", id=1), run("build", id=2)]).state == GREEN


def test_one_required_still_running_is_pending() -> None:
    v = verdict([run("test", id=1), run("build", status="in_progress", conclusion=None, id=2)])
    assert v.state == PENDING
    assert v.waiting_for == ["build"]


def test_missing_required_check_is_pending_even_if_others_passed() -> None:
    v = verdict([run("test", id=1)])
    assert v.state == PENDING
    assert "build" in v.waiting_for


def test_a_failure_is_red_while_others_still_run() -> None:
    v = verdict([run("test", conclusion="failure", id=1), run("build", status="queued", conclusion=None, id=2)])
    assert v.state == RED
    assert [c.name for c in v.failed] == ["test"]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_non_success_conclusions_are_failures(conclusion: str) -> None:
    assert verdict([run("test", conclusion=conclusion, id=1), run("build", id=2)]).state == RED


def test_skipped_is_not_a_failure() -> None:
    assert verdict([run("test", id=1), run("build", id=2), run("docs", conclusion="skipped", id=3)]).state == GREEN


def test_a_rerun_replaces_the_earlier_result() -> None:
    runs = [run("test", conclusion="failure", id=10), run("test", id=11), run("build", id=12)]
    assert [r.id for r in latest_per_name(runs) if r.name == "test"] == [11]
    assert verdict(runs).state == GREEN


def test_a_failing_non_required_check_is_still_red() -> None:
    # Required is the floor for green, not a filter on what counts as failure.
    assert verdict([run("test", id=1), run("build", id=2), run("lint", conclusion="failure", id=3)]).state == RED


# -- trim_log --------------------------------------------------------------------

RAW = "\n".join(
    [
        "﻿2026-09-28T10:00:00.0000000Z ##[group]Run dotnet restore",
        *[f"2026-09-28T10:00:01.0000000Z restoring package {i}" for i in range(300)],
        "2026-09-28T10:00:02.0000000Z ##[endgroup]",
        "2026-09-28T10:00:02.5000000Z ##[group]Run actions/checkout@v4",
        "2026-09-28T10:00:02.6000000Z echo 'on error, print ::error:: and exit'",
        "2026-09-28T10:00:02.7000000Z ##[endgroup]",
        "2026-09-28T10:00:03.0000000Z ##[group]Run grep for forbidden test providers",
        "2026-09-28T10:00:03.1000000Z tests/App.Tests/App.Tests.csproj:10: EntityFrameworkCore.InMemory",
        "2026-09-28T10:00:03.2000000Z ##[error]The EF in-memory provider is forbidden - see AGENT.md",
        "2026-09-28T10:00:03.3000000Z ##[error]Process completed with exit code 1.",
        *[f"2026-09-28T10:00:04.0000000Z post step {i}" for i in range(50)],
    ]
)


def test_trim_keeps_the_error_and_what_led_to_it() -> None:
    out = trim_log(RAW)
    assert "The EF in-memory provider is forbidden" in out
    assert "tests/App.Tests/App.Tests.csproj:10" in out
    assert "Run grep for forbidden test providers" in out


def test_trim_drops_timestamps_markers_and_the_noise_far_from_the_error() -> None:
    out = trim_log(RAW)
    assert "2026-09-28T" not in out
    assert "##[group]" not in out and "##[endgroup]" not in out
    assert "restoring package 0" not in out
    assert "post step 49" not in out
    assert len(out.splitlines()) < 60


def test_trim_anchors_on_the_failing_step_not_on_the_word_error() -> None:
    # A step's script can contain "error" without failing; the failing step is
    # the one that ends in ##[error].
    out = trim_log(RAW)
    assert "on error, print" not in out
    assert out.splitlines()[0] == "Run grep for forbidden test providers"


def test_trim_is_bounded() -> None:
    noisy = "\n".join(f"error CS{i:04d}: something" for i in range(2000))
    assert len(trim_log(noisy, max_lines=200).splitlines()) <= 200


def test_trim_without_error_lines_keeps_the_tail() -> None:
    text = "\n".join(f"line {i}" for i in range(500))
    out = trim_log(text, max_lines=20)
    assert out.splitlines()[-1] == "line 499"
    assert len(out.splitlines()) == 20


# -- fix_brief -----------------------------------------------------------------------


def brief(round_no: int = 1) -> str:
    return fix_brief(
        round_no=round_no,
        max_rounds=2,
        request="Let people search items by title.",
        pr_url="https://github.com/main0034/aaas-app-demo/pull/7",
        branch="feat/search",
        diff_stat=" src/App/Program.cs | 12 +++++",
        failures=[("test", "##[error]The EF in-memory provider is forbidden")],
        runbook="agent/create-app.md",
    )


def test_brief_carries_request_branch_diff_and_log() -> None:
    b = brief()
    assert "Let people search items by title." in b
    assert "feat/search" in b
    assert "src/App/Program.cs" in b
    assert "The EF in-memory provider is forbidden" in b
    assert "pull/7" in b


def test_brief_points_at_the_runbook_and_forbids_a_second_pr() -> None:
    b = brief()
    assert "section 6 of `agent/create-app.md`" in b
    assert "Do not open a new pull request" in b


def test_only_the_last_round_says_so() -> None:
    assert "last round" not in brief(1)
    assert "last round" in brief(2)


def test_parse_pr_url() -> None:
    pr = parse_pr_url("see https://github.com/main0034/aaas-app-demo/pull/12 for details")
    assert (pr.owner, pr.repo, pr.number) == ("main0034", "aaas-app-demo", 12)
    with pytest.raises(ChecksError):
        parse_pr_url("https://github.com/main0034/aaas-app-demo/issues/12")


# -- wait_for_checks ---------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_wait_polls_until_red() -> None:
    clock = FakeClock()
    answers = iter(
        [
            [],
            [run("test", status="in_progress", conclusion=None, id=1)],
            [run("test", conclusion="failure", id=1), run("build", status="in_progress", conclusion=None, id=2)],
        ]
    )
    v, waited = wait_for_checks(lambda: next(answers), sleep=clock.sleep, clock=clock, poll=15, settle=10)
    assert v.state == RED
    assert waited == 10 + 15 + 15


def test_wait_gives_up_with_pending_rather_than_raising() -> None:
    clock = FakeClock()
    v, waited = wait_for_checks(lambda: [], sleep=clock.sleep, clock=clock, timeout=60, poll=15, settle=0)
    assert v.state == PENDING
    assert waited >= 60


def test_wait_survives_a_github_error() -> None:
    clock = FakeClock()
    calls = {"n": 0}

    def fetch() -> list[CheckRun]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChecksError("502")
        return [run("test", id=1), run("build", id=2)]

    v, _ = wait_for_checks(fetch, sleep=clock.sleep, clock=clock, settle=0)
    assert v.state == GREEN


def test_trim_strips_terminal_colours() -> None:
    raw = "2026-09-28T10:00:00.0000000Z \x1b[31;1m  Failed App.Tests.ItemTests.Search [12 ms]\x1b[0m"
    out = trim_log(raw)
    assert "\x1b" not in out
    assert "Failed App.Tests.ItemTests.Search [12 ms]" in out
