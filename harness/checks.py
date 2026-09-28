"""
Fix-forward: wait for a pull request's checks, and turn a red one into a brief.

Phase 5 of POC-PLAN.md. The loop lives here, in the harness, not in the agent's
turn, for two reasons:

* Waiting is not model work. An agent told to "wait for CI" polls with tool
  calls, and every poll carries the whole context (FINDINGS.md #14: the cost is
  context, not output). The harness waits for free.
* The failure is fed to a *fresh* session with the brief, the branch and the
  trimmed log - nothing else. A follow-up in the same conversation pays for the
  original work again; FINDINGS.md #22 measured a focused fresh run at a quarter
  of the original's cost.

Everything that decides something is a pure function over data, so it can be
tested without GitHub: `verdict`, `trim_log`, `fix_brief`, `parse_pr_url`.
`wait_for_checks` takes its fetch and sleep as arguments for the same reason.

Checks are read per commit (`/commits/{sha}/check-runs`), never per PR. Right
after a push, a PR's check list still shows the *previous* commit's red result
for a few seconds, and a loop that reads that sees a failure the agent already
fixed.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

PR_URL_RE = re.compile(r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)")

# The ruleset on aaas-app-demo master requires exactly these two.
DEFAULT_REQUIRED = ("test", "build")

# A check whose conclusion is one of these did not fail.
OK_CONCLUSIONS = {"success", "skipped", "neutral"}

GREEN, RED, PENDING = "green", "red", "pending"


class ChecksError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.slug}/pull/{self.number}"


def parse_pr_url(url: str) -> PullRequest:
    m = PR_URL_RE.search(url)
    if not m:
        raise ChecksError(f"Not a pull request URL: {url}")
    return PullRequest(m["owner"], m["repo"], int(m["number"]))


@dataclass(frozen=True)
class CheckRun:
    id: int
    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None  # success | failure | cancelled | timed_out | skipped | ...

    @property
    def failed(self) -> bool:
        return self.status == "completed" and (self.conclusion or "") not in OK_CONCLUSIONS


@dataclass
class Verdict:
    state: str  # green | red | pending
    failed: list[CheckRun] = field(default_factory=list)
    waiting_for: list[str] = field(default_factory=list)


def latest_per_name(runs: Iterable[CheckRun]) -> list[CheckRun]:
    """A re-run adds a second check run with the same name; the newest wins."""
    latest: dict[str, CheckRun] = {}
    for run in runs:
        if run.name not in latest or run.id > latest[run.name].id:
            latest[run.name] = run
    return list(latest.values())


def verdict(runs: Iterable[CheckRun], required: Iterable[str] = DEFAULT_REQUIRED) -> Verdict:
    """Decide what a commit's checks say.

    Red as soon as any check has failed, even while others still run: the agent
    can start on the failure and the rest will be re-run on its push anyway.
    Green only when every required check has completed and nothing failed. A
    required check that has not appeared yet is pending, not green - a commit
    with no checks at all is the state immediately after a push.
    """
    runs = latest_per_name(runs)
    failed = [r for r in runs if r.failed]
    if failed:
        return Verdict(RED, failed=failed)
    by_name = {r.name: r for r in runs}
    waiting = [n for n in required if n not in by_name or by_name[n].status != "completed"]
    waiting += [r.name for r in runs if r.status != "completed" and r.name not in waiting]
    if waiting:
        return Verdict(PENDING, waiting_for=waiting)
    return Verdict(GREEN)


# -- log trimming ------------------------------------------------------------

_TIMESTAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ERROR = re.compile(
    r"##\[error\]|\berror\b|\bFAILED\b|\bFailed\b|\[FAIL\]|Assert\.|Exception\b|exit code [1-9]",
)
_NOISE = re.compile(r"^##\[(group|endgroup)\]|^\s*$")


def trim_log(text: str, *, before_first_error: int = 40, context: int = 4, max_lines: int = 200) -> str:
    """Cut a job log down to what a person would read to find the cause.

    Keeps: a run-up before the first error line (the command that failed and its
    output), a few lines around every later error line, and never more than
    `max_lines`. Timestamps and group markers are dropped - they are most of a
    raw Actions log and none of its meaning.
    """
    lines = [_TIMESTAMP.sub("", ln.rstrip()) for ln in text.splitlines()]
    lines = [ln for ln in lines if not _NOISE.search(ln)]
    hits = [i for i, ln in enumerate(lines) if _ERROR.search(ln)]
    if not hits:
        kept = lines[-max_lines:]
        return "\n".join(kept)

    keep: set[int] = set(range(max(0, hits[0] - before_first_error), hits[0] + 1))
    for i in hits:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))

    out: list[str] = []
    previous = -2
    for i in sorted(keep):
        if i != previous + 1 and out:
            out.append("   [...]")
        out.append(lines[i])
        previous = i
    if len(out) > max_lines:
        head = max_lines // 2
        out = out[:head] + ["   [... trimmed ...]"] + out[-(max_lines - head - 1):]
    return "\n".join(out)


# -- the brief for a fix round -----------------------------------------------

def fix_brief(
    *,
    round_no: int,
    max_rounds: int,
    request: str,
    pr_url: str,
    branch: str,
    diff_stat: str,
    failures: list[tuple[str, str]],
    runbook: str,
) -> str:
    """The whole of what a fix round's fresh session is told.

    Deliberately small: the original request, where the work is, what changed,
    and the failure. The session re-reads the runbook and AGENT.md itself - they
    are the rules, and a summary of them here would drift.
    """
    names = ", ".join(f"`{name}`" for name, _ in failures)
    logs = "\n\n".join(
        f"### `{name}`\n\n```\n{log.strip()}\n```" for name, log in failures
    )
    last = round_no == max_rounds
    return f"""
# Fix round {round_no} of {max_rounds}

You - an earlier session of you - opened {pr_url} on branch `{branch}` for the
request below. CI failed on it: {names}. The branch is checked out in your working
directory at the commit CI ran against.

## The original request

{request.strip()}

## What the branch changes (`git diff --stat` against the base)

```
{diff_stat.strip() or "(no diff)"}
```

## The failure, trimmed from the Actions log

{logs}

## What to do

Follow section 6 of `{runbook}`, "If CI fails". Work from the log above, not from
a guess about it. Fix the cause on this branch, run the runbook's local checks,
commit, and push to the same branch. Do not open a new pull request, and do not
close or merge this one. If the check that failed is a rule you think is wrong,
do not work around it: say so and stop.

When you have pushed, say in two or three sentences what failed and what you
changed. {"This is the last round. " if last else ""}If you cannot fix it, stop
and explain what you tried and why you think it did not work - a clear
description of a blocker is worth more than a guess.
""".strip()


# -- talking to GitHub --------------------------------------------------------

Runner = Callable[[list[str]], str]


def gh(args: list[str]) -> str:
    """Run gh with the harness's own GH_TOKEN. Not the agent's shell: no policy."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise ChecksError(f"`gh {' '.join(args)}` failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def pr_head(pr: PullRequest, run: Runner = gh) -> dict:
    out = run(["pr", "view", str(pr.number), "-R", pr.slug, "--json", "headRefName,headRefOid,baseRefName,state"])
    return json.loads(out)


def check_runs(pr: PullRequest, sha: str, run: Runner = gh) -> list[CheckRun]:
    out = run(["api", f"repos/{pr.slug}/commits/{sha}/check-runs?per_page=100"])
    data = json.loads(out)
    return [
        CheckRun(id=c["id"], name=c["name"], status=c["status"], conclusion=c.get("conclusion"))
        for c in data.get("check_runs", [])
    ]


def job_log(pr: PullRequest, check: CheckRun, run: Runner = gh) -> str:
    # For GitHub Actions a check run's id is its job id.
    return run(["api", f"repos/{pr.slug}/actions/jobs/{check.id}/logs"])


def wait_for_checks(
    fetch: Callable[[], list[CheckRun]],
    *,
    required: Iterable[str] = DEFAULT_REQUIRED,
    timeout: float = 25 * 60,
    poll: float = 15,
    settle: float = 10,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_poll: Callable[[Verdict, float], None] | None = None,
) -> tuple[Verdict, float]:
    """Poll until the commit's checks are green or red, or `timeout` passes.

    Returns the verdict and the seconds spent waiting. A timeout returns the
    last pending verdict rather than raising: "CI did not finish" is an outcome
    the run record should state, not a crash.
    """
    required = tuple(required)
    started = clock()
    sleep(settle)
    while True:
        try:
            v = verdict(fetch(), required)
        except ChecksError:
            v = Verdict(PENDING, waiting_for=["(GitHub did not answer)"])
        elapsed = clock() - started
        if on_poll:
            on_poll(v, elapsed)
        if v.state != PENDING or elapsed >= timeout:
            return v, elapsed
        sleep(poll)
