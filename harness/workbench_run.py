"""
The workbench's three tasks, as sessions: build, rebuild, ship (harness/workbench.py).

    ./run.sh --task build   --app-repo aaas-app-demo --change item-archive
    ./run.sh --task rebuild --app-repo aaas-app-demo --change item-archive
    ./run.sh --task ship    --app-repo aaas-app-demo --change item-archive --spec-testers 2 --fix-rounds 2 --auto-merge

`aaas-agent/workbench.sh` wraps these for the operator's checkout and Rider.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import change as chg
from . import checks as ck
from . import intake
from . import workbench as wb
from . import workspace as ws
from .main import (
    TASKS,
    StatusLine,
    _delta,
    _options,
    _snapshot,
    app_briefing,
    finish_change,
    run_session,
    run_spec_tester,
)
from .policy import APP_PROTECTED_GLOBS, WORKBENCH_OFFLINE_REASON, ToolPolicy
from .report import RunRecord

BUILDER_RUNBOOK = TASKS["create-change"]
SPEC_RUNBOOK = TASKS["write-acceptance"]


def _start(args: argparse.Namespace) -> tuple[str, Path, ws.Workspace | None]:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.runs_dir) / run_id
    print(f"\033[1mRun {run_id}\033[0m  ({run_dir})  {args.task} {args.change}\n")
    print("Preparing workspace…")
    try:
        workspace = ws.prepare(run_dir / "workspace", owner=args.owner, app_repo=args.app_repo)
    except ws.WorkspaceError as exc:
        print(f"\033[31m{exc}\033[0m", file=sys.stderr)
        return run_id, run_dir, None
    for rb in (BUILDER_RUNBOOK, SPEC_RUNBOOK):
        if not (workspace.deployments / rb).is_file():
            print(f"\033[31m{rb} does not exist in the deployments repo.\033[0m", file=sys.stderr)
            return run_id, run_dir, None
    return run_id, run_dir, workspace


def _base(workspace: ws.Workspace) -> str:
    return ws._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace.app)  # noqa: SLF001


def _summary(record: RunRecord, run_dir: Path, extra: list[str]) -> None:
    minutes, seconds = divmod(int(record.seconds), 60)
    print(f"\n\033[1mDone in {minutes}m {seconds}s\033[0m - ${record.cost_usd:.4f}")
    for line in extra:
        print(f"  {line}")
    print(f"  Report: {run_dir / 'report.md'}")
    print(f"\033[1mEnded because:\033[0m {record.end_reason or '(not recorded)'}")


async def run_build(args: argparse.Namespace, result: dict | None = None) -> int:
    """build and rebuild: one builder session on the change branch, pushed by the harness."""
    result = result if result is not None else {}
    rebuild = args.task == "rebuild"
    run_id, run_dir, workspace = _start(args)
    if workspace is None:
        return 2
    base = _base(workspace)
    today = datetime.now(timezone.utc).date()
    try:
        branch = wb.find_branch(workspace.app, args.change, today)
        if rebuild and not branch.exists:
            raise wb.WorkbenchError(f"no branch for {args.change!r} on origin: build it first")
        start = wb.checkout(workspace.app, branch, base, args.change, args.request)
    except (wb.WorkbenchError, ws.WorkspaceError) as exc:
        print(f"\033[31m{exc}\033[0m", file=sys.stderr)
        return 2
    request = (workspace.app / wb.request_path(args.change)).read_text(encoding="utf-8")
    message = request
    # build and rebuild differ only in what they expect: a branch with a build on it
    # is rebuilt from the change in its request, whichever was asked for.
    built = wb.last_build(workspace.app, args.change) if branch.exists else None
    rebuild = built is not None
    if built is not None:
        if built[1] == wb.request_blob(workspace.app, args.change):
            print(
                f"\033[31m{wb.request_path(args.change)} is unchanged since the last build ({built[0][:7]}).\033[0m "
                "Edit it, commit and push to the branch, then rebuild.",
                file=sys.stderr,
            )
            return 2
        old = wb.read_blob(workspace.app, built[1])
        stat = ws._run(["git", "diff", "--stat", f"origin/{base}...HEAD"], cwd=workspace.app)  # noqa: SLF001
        message = wb.rebuild_message(args.change, request, old, stat, base)

    policy = ToolPolicy(
        writable_roots=[workspace.app, Path("/tmp")], protected_globs=APP_PROTECTED_GLOBS, offline=True,
        offline_reason=WORKBENCH_OFFLINE_REASON, offline_git_reason=WORKBENCH_OFFLINE_REASON,
    )
    prompt = (workspace.deployments / "agent" / "PROMPT.md").read_text(encoding="utf-8")
    append = prompt + "\n\n" + app_briefing(workspace, BUILDER_RUNBOOK) + "\n\n" + wb.build_note(branch, args.change)
    opts = _options(append, policy, workspace.app, workspace, {"GH_TOKEN": ""}, args)
    record = RunRecord(
        run_id=run_id, directory=run_dir, task=args.task, request=message,
        metadata={
            "change": branch.cid, "branch": branch.name, "kind": "rebuild" if rebuild else "build",
            "start": start[:7], "runbook": f"{BUILDER_RUNBOOK} (sections 1-4)",
            "model": args.model or "(CLI default)",
            "auth": "subscription" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api-key",
        },
    )
    status = StatusLine()
    code = 1
    outcome = None
    try:
        print("\033[1m── builder " + "─" * 61 + "\033[0m")
        before = _snapshot(record)
        outcome = await run_session(opts, message, record, status, interactive=False)
        record.rounds.append({"round": 0, "kind": record.metadata["kind"], **_delta(record, before)})
        if outcome.stopped_by:
            record.end_reason = f"the builder's session ended early ({outcome.stopped_by}); nothing was pushed"
        else:
            sha, changed = wb.commit_build(workspace.app, args.change, start, rebuild)
            code_files = [f for f in changed if not f.startswith(str(chg.REQUESTS_DIR))]
            if not code_files:
                record.end_reason = "the builder changed nothing; nothing was pushed"
            else:
                wb.push(workspace.app, branch)
                record.metadata["pushed"] = sha[:7]
                record.end_reason = f"pushed {branch.name} @ {sha[:7]} ({len(code_files)} files changed)"
                result.update(branch=branch.name, sha=sha, files=code_files, summary=outcome.last_text)
                code = 0
    except (wb.WorkbenchError, ws.WorkspaceError) as exc:
        record.end_reason = f"harness error: {exc}"
    finally:
        record.finish(policy.denials)
    _summary(record, run_dir, [
        f"Branch: {branch.name}",
        "Preview it: in the app repo, `git fetch && git checkout " + branch.name + " && git pull`, then Preview",
    ] if code == 0 else [])
    if code == 0 and outcome and outcome.last_text:
        print("\n\033[1mThe builder's summary\033[0m\n" + outcome.last_text.strip())
    return code


async def run_ship(args: argparse.Namespace, progress=None, result: dict | None = None) -> int:  # noqa: ANN001
    """ship: spec-testers from the final request, the record, the PR, then create-change's tail."""
    say = progress or (lambda _s: None)
    result = result if result is not None else {}
    run_id, run_dir, workspace = _start(args)
    if workspace is None:
        return 2
    base = _base(workspace)
    try:
        branch = wb.find_branch(workspace.app, args.change, datetime.now(timezone.utc).date())
        if not branch.exists:
            raise wb.WorkbenchError(f"no branch for {args.change!r} on origin: build it first")
        wb.checkout(workspace.app, branch, base, args.change, None)
        problems = wb.ship_ready(workspace.app, args.change)
        request = (workspace.app / wb.request_path(args.change)).read_text(encoding="utf-8")
        problems += [f"the request is not complete: {p}" for p in intake.check_request(request)]
        if (workspace.app / chg.CHANGES_DIR / branch.cid).exists():
            problems.append(f"{chg.CHANGES_DIR / branch.cid} already exists on the branch")
    except (wb.WorkbenchError, ws.WorkspaceError) as exc:
        problems = [str(exc)]
    if problems:
        for p in problems:
            print(f"\033[31m{p}\033[0m", file=sys.stderr)
        return 2
    args.request = request
    cid = branch.cid

    spec_prompt = (workspace.deployments / SPEC_RUNBOOK).read_text(encoding="utf-8")
    builder_prompt = (workspace.deployments / "agent" / "PROMPT.md").read_text(encoding="utf-8")
    build_policy = ToolPolicy(writable_roots=[workspace.app, Path("/tmp")], protected_globs=APP_PROTECTED_GLOBS)

    def build_options(fix: bool = False):  # noqa: ANN202 - fix rounds only, on the open PR
        append = builder_prompt + "\n\n" + app_briefing(workspace, BUILDER_RUNBOOK, fix=True)
        return _options(append, build_policy, workspace.app, workspace,
                        {"GH_TOKEN": os.environ.get("GH_TOKEN", "")}, args)

    letters = [chr(ord("A") + i) for i in range(args.spec_testers)] if args.spec_testers > 1 else [""]
    record = RunRecord(
        run_id=run_id, directory=run_dir, task=args.task, request=request,
        metadata={
            "change": cid, "branch": branch.name, "spec-testers": args.spec_testers,
            "builds": wb.build_count(workspace.app, args.change),
            "deployments HEAD": ws.head_sha(workspace.deployments),
            f"{args.app_repo} {branch.name}": ws.head_sha(workspace.app),
            "runbooks": f"{SPEC_RUNBOOK}; fix rounds {BUILDER_RUNBOOK}",
            "model": args.model or "(CLI default)",
            "auth": "subscription" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api-key",
        },
    )
    status = StatusLine()
    held: dict[str, bytes] = {}
    spec_records: list = []
    scratch: list[Path] = []

    async def steps() -> int:
        say("Writing down how we will check it works")
        for letter in letters:
            problem = await run_spec_tester(
                args, workspace, cid, letter, spec_prompt, SPEC_RUNBOOK, run_dir, run_id, record, status,
                held, spec_records, scratch,
            )
            if problem:
                record.end_reason = f"{problem}; nothing was shipped"
                return 1

        acc_out = run_dir / "acceptance"
        acc_out.mkdir(parents=True, exist_ok=True)
        for name, data in held.items():
            (acc_out / name).write_bytes(data)
        hist = wb.history(workspace.app, args.change, base)
        builds = record.metadata["builds"]
        tests_sha = wb.record(workspace.app, branch, args.change, sorted(acc_out.iterdir()))
        wb.push(workspace.app, branch)
        record.metadata["tests commit"] = tests_sha[:7]
        print(f"\n\033[1mRequest and acceptance tests recorded on {branch.name} @ {tests_sha[:7]}\033[0m")

        body = run_dir / "pr-body.md"
        body.write_text(wb.pr_body(branch, args.change, request, hist, builds), encoding="utf-8")
        url = ck.gh([
            "pr", "create", "-R", f"{args.owner}/{args.app_repo}", "--base", base, "--head", branch.name,
            "--title", f"feat: {wb.title(request) or args.change}", "--body-file", str(body),
        ]).strip().splitlines()[-1]
        record.pr_urls.append(url)
        pr = ck.parse_pr_url(url)
        result["pr"] = pr
        print(f"\033[1mPR {url}\033[0m")
        # fix_forward annotates the latest round with what CI said about it.
        record.rounds.append({"round": 0, "kind": f"workbench ({builds} builds)", "sha": tests_sha[:7]})
        return await finish_change(
            args, pr, base, branch.name, cid, tests_sha, workspace, BUILDER_RUNBOOK, build_options,
            record, status, say, result,
        )

    try:
        exit_code = await steps()
    except (chg.ChangeError, ws.WorkspaceError, ck.ChecksError, wb.WorkbenchError) as exc:
        record.end_reason = f"harness error: {exc}"
        exit_code = 1
    finally:
        for d in scratch:
            shutil.rmtree(d, ignore_errors=True)
        denials = list(build_policy.denials)
        for rec, policy in spec_records:
            rec.finish(policy.denials)
            denials += policy.denials
        record.finish(denials)
        result.update(end_reason=record.end_reason, cost_usd=record.cost_usd, seconds=record.seconds,
                      change=cid, app=workspace.app)
    _summary(record, run_dir, [f"PR: {u}" for u in record.pr_urls])
    return exit_code
