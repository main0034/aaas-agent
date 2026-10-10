"""
The AaaS agent harness.

Runs one task, in a container, against a fresh clone, with a tool policy and a
run record. Phase 4a of POC-PLAN.md: prompt -> deployment PR, no code
generation, no Terraform, no Azure.

Design notes worth keeping:

* The system prompt is NOT written here. It lives in `agent/PROMPT.md` in the
  deployments repo, next to the runbooks, under CODEOWNERS. The harness reads
  it out of the checkout it just made, which means the prompt is versioned with
  the guardrails it describes and the agent cannot edit it.

* `permission_mode` is `bypassPermissions`, which sounds alarming and is the
  right answer here. There is no human at a terminal to answer a permission
  prompt mid-run, so an "ask" is a hang. The gate is the PreToolUse hook, and
  the boundary is the container. Making the mode more timid would not add
  safety, it would add stalls.

* `setting_sources=None` so nothing from ~/.claude leaks into the run. A run
  that behaves differently on your laptop than in CI is not a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)

from . import change as chg
from . import checks as ck
from . import workspace as ws
from .workspace_pr import checkout_pr_branch, diff_stat
from .policy import (
    ACCEPTANCE_PROTECTED_GLOBS,
    APP_PROTECTED_GLOBS,
    DEPLOYMENTS_PROTECTED_GLOBS,
    ToolPolicy,
    make_pre_tool_use_hook,
)
from .report import RunRecord

ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite"]
DISALLOWED_TOOLS = ["WebFetch", "WebSearch", "Task", "NotebookEdit"]

TASKS = {
    "create-deployment": "agent/create-deployment.md",
    "create-app": "agent/create-app.md",
    "write-acceptance": "agent/write-acceptance.md",
    # Both of the above, in order, on one change record (harness/change.py). The
    # builder follows create-app.md; the spec-tester write-acceptance.md.
    "create-change": "agent/create-app.md",
}

# Tasks that work in an application repository.
APP_TASKS = {"create-app", "write-acceptance", "create-change"}

ACCEPTANCE_DIR = Path("tests/App.Tests/Acceptance")
ACCEPTANCE_BASE = Path(__file__).parent / "acceptance" / "AcceptanceBase.cs"


def briefing(
    task: str, workspace: ws.Workspace, runbook: str, fix: bool = False, skip_local_checks: bool = False
) -> str:
    if task in {"create-app", "create-change"}:
        return app_briefing(workspace, runbook, fix, skip_local_checks)
    if task == "write-acceptance":
        return acceptance_briefing(workspace)
    return f"""
# Workspace

You are running in a container. Nothing here is on anyone's laptop, and there is
no Azure credential anywhere in this environment - not a service principal, not
an `az` login, not an environment variable. That is deliberate and it is the
reason the design is safe, so do not go looking for one.

| Path | You may |
|---|---|
| `{workspace.deployments}` | read and write, except the guardrail paths below |
| `{workspace.reference}` | read only - reference copies of the other repositories |
| `/tmp` | write scratch files, such as a pull request body |

Guardrail paths inside the deployments checkout - `schemas/`, `scripts/`,
`.github/`, `agent/` - are readable and not writable. If one of them looks
wrong, say so and stop.

Your shell has `git`, `gh`, `jq`, and `python3` restricted to running
`scripts/validate_deployment.py`. There is no `terraform` and no `az`. Use the
Write and Edit tools to change files rather than shell redirection - shell
redirection is refused.

# Your task

Follow `{runbook}` in the deployments checkout, exactly. Read it before you
start. It is the runbook, not a summary of one.

Read `{workspace.reference}/aaas-infra-modules/modules/app-stack/README.md` for
what the module accepts. Where it and the JSON Schema disagree, the schema wins
and you should say so.

# When to stop

Stop when you have opened the pull request and can report its URL, or when you
are blocked and can say precisely what is blocking you. Do not merge anything.

If you need something from the requester that you cannot infer - `owner` and
`costCenter` above all - ask for it plainly and wait. Asking is not failing.
""".strip()


APP_STOP = """
# When to stop

Stop when you have opened the pull request and can report its URL, or when you
are blocked and can say precisely what is blocking you. Do not merge anything.

If the request leaves something about the data model genuinely open, ask
plainly and wait. Asking is not failing.
"""

FIX_STOP = """
# When to stop

This is a fix round on a pull request that already exists. Stop when you have
pushed a fix to its branch, or when you are blocked and can say precisely what
is blocking you. Do not open another pull request, and do not merge or close
this one. Nobody will answer a question in this round, so do not ask one.
"""


# Phase 5's deterministic fallback for producing a red check: the initial
# session pushes without running the runbook's local proof, so CI meets the code
# first. Fix rounds never get this - they run section 4 as written.
SKIP_LOCAL_CHECKS = """
# For this run only

Skip section 4 of the runbook ("Prove it before anyone looks at it"): do not run
the local restore, format, build or test commands before you push. CI runs the
same checks on the pull request. This is deliberate - the run is measuring how
a failed check gets fixed - so do not compensate by checking some other way.
"""


def app_briefing(
    workspace: ws.Workspace, runbook: str, fix: bool = False, skip_local_checks: bool = False
) -> str:
    stop = FIX_STOP if fix else APP_STOP
    if skip_local_checks and not fix:
        stop = SKIP_LOCAL_CHECKS.strip() + "\n\n" + stop.strip()
    return f"""
# Workspace

You are running in a container. Nothing here is on anyone's laptop, and there is
no Azure credential anywhere in this environment - not a service principal, not
an `az` login, not an environment variable. That is deliberate and it is the
reason the design is safe, so do not go looking for one.

| Path | You may |
|---|---|
| `{workspace.app}` | read and write, except the guardrail paths below. Your working directory |
| `{workspace.deployments}` | read only - the runbook and the deployment this app feeds |
| `{workspace.reference}` | read only - reference copies of the other repositories |
| `/tmp` | write scratch files, such as a pull request body |

Guardrail paths inside the application checkout - the ones `AGENT.md` lists
under "Do not edit these", plus `dotnet-tools.json` - are readable and not
writable. If one of them looks wrong, say so and stop.

Your shell has `git`, `gh`, `jq` and `dotnet` (the .NET SDK the repository's
`global.json` pins, and the `dotnet-ef` tool after `dotnet tool restore`). There
is no database, no `terraform` and no `az`. Use the Write and Edit tools to
change files rather than shell redirection - shell redirection is refused.

# Your task

Follow `{workspace.deployments}/{runbook}`, exactly. Read it before you start. It
is the runbook, not a summary of one. The application repository is
`{workspace.app.name if workspace.app else ''}`, already cloned at `{workspace.app}`.

{stop.strip()}
""".strip()


def acceptance_briefing(workspace: ws.Workspace, namespace: str = "Acceptance") -> str:
    acc = workspace.app / ACCEPTANCE_DIR
    return f"""
# Workspace

You are running in a container with no credentials of any kind and no network
access to GitHub. That is deliberate: see "Why you cannot see the change" in your
instructions.

| Path | You may |
|---|---|
| `{workspace.app}` | read only - the application as it is *before* the change. Your working directory |
| `{acc}` | write your test file and `NOTES.md` here, and nowhere else. `AcceptanceBase.cs` is already there and is not yours to edit |
| `/tmp` | write scratch files |

Your shell has `git` (local commands only), `jq`, the usual read-only tools and
`dotnet` (restore, build, test). There is no database, so endpoint tests skip
here; that is expected. Use the Write and Edit tools to create files.

# Your task

The request is the next message. Write acceptance tests for it, following your
instructions exactly. Your file's namespace is `{namespace}`. Stop when `dotnet build`
succeeds with your file in place and `NOTES.md` is written, and report the number
of tests and the ambiguities you left untested.
""".strip()


def render_block(block) -> str:  # noqa: ANN001
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, ThinkingBlock):
        return ""
    if isinstance(block, ToolUseBlock):
        detail = block.input.get("command") or block.input.get("file_path") or block.input.get("pattern") or ""
        return f"  \033[2m→ {block.name}({str(detail)[:110]})\033[0m"
    return ""


class StatusLine:
    """A heartbeat printed while the agent is thinking.

    Silence and a hang look identical from the outside, and a single turn can
    spend a minute deciding what to do before it emits a visible token. The
    first run of this harness produced 85 seconds of nothing after the prompt
    was sent, which is long enough to conclude something is broken and reach
    for Ctrl-C.

    Prints on the same line and erases itself before any real output, so the
    transcript stays clean.
    """

    def __init__(self, idle_after: float = 2.0) -> None:
        self._task: asyncio.Task | None = None
        self._idle_after = idle_after
        self._started = 0.0
        self._last = 0.0
        self._shown = False

    def start(self) -> None:
        self._started = self._last = time.time()
        self._task = asyncio.create_task(self._beat())

    async def _beat(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.4)
                if time.time() - self._last >= self._idle_after:
                    elapsed = int(time.time() - self._started)
                    sys.stdout.write(f"\r\033[2m  working... {elapsed}s\033[0m\033[K")
                    sys.stdout.flush()
                    self._shown = True
        except asyncio.CancelledError:
            pass

    def _erase(self) -> None:
        if self._shown:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self._shown = False

    def write(self, text: str) -> None:
        self._erase()
        print(text)
        self._last = time.time()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._erase()


def read_followup() -> str | None:
    """Return the next message, or None to end the session.

    A blank line deliberately does NOT end the session. It used to, and a stray
    Enter while waiting then closed a run that had real work in it. Ending is
    now something you have to mean.
    """
    while True:
        try:
            raw = input("\n\033[1mYou\033[0m  (type 'done' to finish): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() in {"done", "exit", "quit", "/done", "/exit", "/quit"}:
            return None
        if raw:
            print()
            return raw
        print("\033[2m  (blank line ignored - type 'done' when you want to stop)\033[0m")


async def run(args: argparse.Namespace) -> int:
    if args.task == "create-change":
        return await run_change(args)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.runs_dir) / run_id
    print(f"\033[1mRun {run_id}\033[0m  ({run_dir})\n")

    print("Preparing workspace…")
    if args.task in APP_TASKS and not args.app_repo:
        print(f"\033[31m--task {args.task} needs --app-repo <name>.\033[0m", file=sys.stderr)
        return 2
    acceptance = args.task == "write-acceptance"

    try:
        workspace = ws.prepare(
            run_dir / "workspace",
            owner=args.owner,
            app_repo=args.app_repo if args.task in APP_TASKS else None,
            isolate_app=acceptance,
        )
    except ws.WorkspaceError as exc:
        print(f"\033[31m{exc}\033[0m", file=sys.stderr)
        return 2

    runbook = TASKS[args.task]
    runbook_path = workspace.deployments / runbook
    if not runbook_path.is_file():
        print(
            f"\033[31m{runbook} does not exist in the deployments repo.\033[0m\n"
            f"The harness does not carry its own copy on purpose: the runbook is a "
            f"guardrail and belongs next to the schema it describes.",
            file=sys.stderr,
        )
        return 2

    if acceptance:
        # A different role, so a different prompt: PROMPT.md describes the agent that
        # builds and opens PRs. This one only writes tests, from the runbook alone.
        system_prompt = runbook_path.read_text(encoding="utf-8")
        acc_dir = workspace.app / ACCEPTANCE_DIR
        acc_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(ACCEPTANCE_BASE, acc_dir / ACCEPTANCE_BASE.name)
    else:
        prompt_path = workspace.deployments / "agent" / "PROMPT.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")

    # /tmp is writable because the runbooks tell the agent to compose a PR body
    # in a file - a heredoc inside an argument is a reliable way to produce a
    # confusing shell error, which is why release.yml does the same thing.
    if acceptance:
        policy = ToolPolicy(
            writable_roots=[workspace.app / ACCEPTANCE_DIR, Path("/tmp")],
            protected_globs=ACCEPTANCE_PROTECTED_GLOBS,
            offline=True,
        )
    else:
        policy = ToolPolicy(
            writable_roots=[*workspace.writable_roots, Path("/tmp")],
            protected_globs=APP_PROTECTED_GLOBS if workspace.app else DEPLOYMENTS_PROTECTED_GLOBS,
        )
    workdir = workspace.app or workspace.deployments
    read_only_dirs = [str(workspace.reference)]
    if workspace.app:
        read_only_dirs.append(str(workspace.deployments))

    def make_options(fix: bool = False) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt + "\n\n" + briefing(args.task, workspace, runbook, fix, args.skip_local_checks),
            },
            allowed_tools=ALLOWED_TOOLS,
            disallowed_tools=DISALLOWED_TOOLS,
            permission_mode="bypassPermissions",
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_hook(policy)])],
            },
            cwd=str(workdir),
            add_dirs=read_only_dirs,
            max_turns=args.max_turns,
            model=args.model,
            setting_sources=None,
            # No token for write-acceptance: it has nothing to push and nothing to read.
            env={"GH_TOKEN": "" if acceptance else os.environ.get("GH_TOKEN", "")},
        )

    options = make_options()

    record = RunRecord(
        run_id=run_id,
        directory=run_dir,
        task=args.task,
        request=args.request,
        metadata={
            "deployments HEAD": ws.head_sha(workspace.deployments),
            **(
                {f"{workspace.app.name} HEAD": ws.head_sha(workspace.app)}
                if workspace.app
                else {}
            ),
            "runbook": runbook,
            "model": args.model or "(CLI default)",
            **({"skip local checks": "yes (Phase 5 fallback)"} if args.skip_local_checks else {}),
            "auth": "subscription" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api-key",
        },
    )

    print(f"Workspace ready at {workdir}\n")
    print("\033[1m" + "─" * 72 + "\033[0m")

    exit_code = 0
    status = StatusLine()

    # try/finally: an interrupted run should still leave a report behind. The
    # transcript is written as it goes, but the timings and refusal list are
    # only assembled at the end.
    try:
        before = _snapshot(record)
        outcome = await run_session(options, args.request, record, status, interactive=not args.non_interactive)
        record.rounds.append({"round": 0, "kind": "initial", **_delta(record, before)})
        if outcome.is_error:
            exit_code = 1

        if args.fix_rounds and outcome.stopped_by:
            record.end_reason = f"the agent's session ended early ({outcome.stopped_by}); no fix-forward"
        elif args.fix_rounds:
            pr_url = next((u for u in record.pr_urls if f"/{args.app_repo}/pull/" in u), None)
            if pr_url is None:
                record.end_reason = "no pull request on the app repository, so nothing to fix forward"
                exit_code = exit_code or 1
            else:
                exit_code = await fix_forward(
                    args, ck.parse_pr_url(pr_url), workspace, runbook, make_options, record, status
                ) or exit_code
        elif outcome.stopped_by:
            record.end_reason = f"the agent's session ended early: {outcome.stopped_by}"
        elif acceptance:
            written = collect_acceptance(workspace.app / ACCEPTANCE_DIR, run_dir / "acceptance")
            record.metadata["acceptance files"] = ", ".join(written) or "(none)"
            if any(name.endswith(".cs") for name in written):
                record.end_reason = f"acceptance tests written: {', '.join(written)}"
            else:
                record.end_reason = "the agent stopped without writing a test file"
                exit_code = exit_code or 1
        elif record.pr_urls:
            record.end_reason = "pull request opened"
        else:
            record.end_reason = "the agent stopped without opening a pull request"
    finally:
        record.finish(policy.denials)

    minutes, seconds = divmod(int(record.seconds), 60)
    print(
        f"\n\033[1mDone in {minutes}m {seconds}s\033[0m - ${record.cost_usd:.4f}, "
        f"{len(record.turns)} exchange(s), {record.sdk_turns} model turns"
    )
    if acceptance:
        print(f"  Tests: {run_dir / 'acceptance'}")
    elif record.pr_urls:
        for url in record.pr_urls:
            print(f"  PR: {url}")
    else:
        print("  \033[33mNo pull request URL appeared in the output.\033[0m")
        exit_code = exit_code or 1
    if policy.denials:
        print(f"  {len(policy.denials)} policy refusal(s) — see report.md")
    print(f"  Report: {run_dir / 'report.md'}")
    # The last line says why, so a log tail is enough (FINDINGS.md #22).
    print(f"\033[1mEnded because:\033[0m {record.end_reason or '(not recorded)'}")

    return exit_code


def spec_tmp_dir() -> str:
    """Where the spec-tester's checkout and CLI store go: not /tmp.

    /tmp is a writable root for both roles (PR bodies are composed there), so a
    checkout under /tmp would be writable in full - the policy would no longer
    confine the spec-tester to its Acceptance directory. The first create-change
    run had it there (20261010T130041Z).
    """
    return str(Path.home())


async def run_change(args: argparse.Namespace) -> int:
    """create-change: brief -> spec-tester -> builder -> hidden tests -> fix rounds.

    One change record (harness/change.py), two roles in one container. The order
    is what keeps them independent:

    * the spec-tester runs first, in an isolated checkout of master, before any
      code for the change exists anywhere
    * its tests are then held in memory only - its checkout, its CLI session
      store and its transcript are not on disk while the builder runs
    * the builder never sees them; the harness commits them after its push, and
      from then on CI runs them and fix rounds may read them
    """
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.runs_dir) / run_id
    cid = chg.change_id(args.change, datetime.now(timezone.utc).date())
    namespace = chg.namespace_for(cid)
    branch = chg.branch_for(cid)
    print(f"\033[1mRun {run_id}\033[0m  ({run_dir})\n\033[1mChange {cid}\033[0m  (branch {branch})\n")

    print("Preparing workspace…")
    try:
        workspace = ws.prepare(run_dir / "workspace", owner=args.owner, app_repo=args.app_repo)
        # Outside run_dir, so nothing of it is left under the mounted directory.
        spec_root = Path(tempfile.mkdtemp(prefix="spec-", dir=spec_tmp_dir()))
        spec_app = ws.clone_isolated(spec_root / args.app_repo, args.owner, args.app_repo)
    except ws.WorkspaceError as exc:
        print(f"\033[31m{exc}\033[0m", file=sys.stderr)
        return 2

    builder_runbook, spec_runbook = TASKS["create-change"], TASKS["write-acceptance"]
    for rb in (builder_runbook, spec_runbook):
        if not (workspace.deployments / rb).is_file():
            print(f"\033[31m{rb} does not exist in the deployments repo.\033[0m", file=sys.stderr)
            return 2
    builder_prompt = (workspace.deployments / "agent" / "PROMPT.md").read_text(encoding="utf-8")
    spec_prompt = (workspace.deployments / spec_runbook).read_text(encoding="utf-8")

    acc_dir = spec_app / ACCEPTANCE_DIR
    acc_dir.mkdir(parents=True, exist_ok=True)
    if not (acc_dir / ACCEPTANCE_BASE.name).exists():
        # Older app repos do not carry the base class yet.
        shutil.copy(ACCEPTANCE_BASE, acc_dir / ACCEPTANCE_BASE.name)
    spec_ws = ws.Workspace(root=spec_root, deployments=workspace.deployments, reference=workspace.reference, app=spec_app)

    spec_policy = ToolPolicy(
        writable_roots=[acc_dir, Path("/tmp")], protected_globs=ACCEPTANCE_PROTECTED_GLOBS, offline=True
    )
    build_policy = ToolPolicy(writable_roots=[workspace.app, Path("/tmp")], protected_globs=APP_PROTECTED_GLOBS)
    # The spec-tester's own CLI session store, deleted after it: the CLI keeps every
    # session's tool calls - the test file included - under its config directory.
    spec_config = Path(tempfile.mkdtemp(prefix="spec-claude-", dir=spec_tmp_dir()))

    def options(role: str, fix: bool = False) -> ClaudeAgentOptions:
        if role == "spec":
            append = spec_prompt + "\n\n" + acceptance_briefing(spec_ws, namespace)
            policy, cwd = spec_policy, spec_app
            add_dirs = [str(workspace.reference), str(workspace.deployments)]
            env = {"GH_TOKEN": "", "CLAUDE_CONFIG_DIR": str(spec_config)}
        else:
            append = builder_prompt + "\n\n" + app_briefing(workspace, builder_runbook, fix, False)
            if not fix:
                append += "\n\n" + chg.builder_note(cid)
            policy, cwd = build_policy, workspace.app
            add_dirs = [str(workspace.reference), str(workspace.deployments)]
            env = {"GH_TOKEN": os.environ.get("GH_TOKEN", "")}
        return ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
            allowed_tools=ALLOWED_TOOLS,
            disallowed_tools=DISALLOWED_TOOLS,
            permission_mode="bypassPermissions",
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_hook(policy)])]},
            cwd=str(cwd),
            add_dirs=add_dirs,
            max_turns=args.max_turns,
            model=args.model,
            setting_sources=None,
            env=env,
        )

    record = RunRecord(
        run_id=run_id,
        directory=run_dir,
        task=args.task,
        request=args.request,
        metadata={
            "change": cid,
            "branch": branch,
            "deployments HEAD": ws.head_sha(workspace.deployments),
            f"{args.app_repo} HEAD": ws.head_sha(workspace.app),
            "runbooks": f"{spec_runbook}, then {builder_runbook}",
            "model": args.model or "(CLI default)",
            "auth": "subscription" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api-key",
        },
    )
    spec_record = RunRecord(
        run_id=f"{run_id}-spec", directory=run_dir / "spec", task="write-acceptance",
        request=args.request, defer_writes=True,
        metadata={"change": cid, "namespace": namespace, "runbook": spec_runbook},
    )
    status = StatusLine()
    held: dict[str, bytes] = {}

    async def steps() -> int:
        record.metadata["brief commit"] = chg.start_branch(workspace.app, cid, args.request)[:7]

        # 1. The spec-tester, before any code for the change exists.
        print("\033[1m── spec-tester " + "─" * 57 + "\033[0m")
        before = _snapshot(spec_record)
        outcome = await run_session(options("spec"), args.request, spec_record, status, interactive=False)
        spec_round = {"round": "-", "kind": "spec-tester", **_delta(spec_record, before)}
        for f in sorted(acc_dir.glob("*")):
            if f.is_file() and f.name != ACCEPTANCE_BASE.name:
                held[f.name] = f.read_bytes()
        shutil.rmtree(spec_root, ignore_errors=True)
        shutil.rmtree(spec_config, ignore_errors=True)
        spec_round["files"] = sorted(held)
        record.rounds.append(spec_round)
        record.cost_usd += spec_record.cost_usd
        if outcome.stopped_by or not any(n.endswith(".cs") for n in held):
            record.end_reason = (
                f"the spec-tester wrote no test file ({outcome.stopped_by or 'stopped'}); the builder did not run"
            )
            return 1

        # 2. The builder. Nothing of the spec-tester's is on disk now.
        print("\033[1m── builder " + "─" * 61 + "\033[0m")
        before = _snapshot(record)
        outcome = await run_session(options("build"), args.request, record, status, interactive=False)
        record.rounds.append({"round": 0, "kind": "initial", **_delta(record, before)})
        if outcome.stopped_by:
            record.end_reason = f"the builder's session ended early ({outcome.stopped_by})"
            return 1
        pr_url = next((u for u in record.pr_urls if f"/{args.app_repo}/pull/" in u), None)
        if pr_url is None:
            record.end_reason = "the builder opened no pull request on the app repository"
            return 1
        pr = ck.parse_pr_url(pr_url)
        head = ck.pr_head(pr)
        if head["headRefName"] != branch:
            record.end_reason = f"the builder's PR is on `{head['headRefName']}`, not the change branch `{branch}`"
            return 1

        # 3. The hidden tests join the branch. From here CI runs them.
        acc_out = run_dir / "acceptance"
        acc_out.mkdir(parents=True, exist_ok=True)
        for name, data in held.items():
            (acc_out / name).write_bytes(data)
        checkout_pr_branch(workspace.app, branch, head["headRefOid"])
        tests_sha = chg.add_tests(workspace.app, cid, sorted(acc_out.iterdir()))
        record.metadata["tests commit"] = tests_sha[:7]
        print(f"\n\033[1mAcceptance tests committed to {branch} @ {tests_sha[:7]}\033[0m")
        # GitHub updates a PR's head a few seconds after the push. Reading it sooner
        # judged the builder's commit, without the tests, and started a fix round on
        # it (20261010T130041Z).
        if not ck.wait_for_head(pr, tests_sha, timeout=args.checks_timeout):
            record.end_reason = f"the PR's head never became the tests commit {tests_sha[:7]}"
            return 1

        # 4. Checks, and fix rounds that may read the tests but not change them.
        code = await fix_forward(
            args, pr, workspace, builder_runbook,
            lambda fix=False: options("build", fix), record, status, note=chg.fix_note(cid),
        )
        if code == 0:
            final = ck.pr_head(pr)["headRefOid"]
            touched = chg.verify_untouched(workspace.app, cid, tests_sha, final)
            if touched:
                record.end_reason = f"green, but {', '.join(touched)} changed after the harness committed them"
                return 1
            else:
                record.end_reason += "; acceptance tests unchanged since the harness committed them"
        return code

    try:
        exit_code = await steps()
    except (chg.ChangeError, ws.WorkspaceError, ck.ChecksError) as exc:
        record.end_reason = f"harness error: {exc}"
        exit_code = 1
    finally:
        shutil.rmtree(spec_root, ignore_errors=True)
        shutil.rmtree(spec_config, ignore_errors=True)
        spec_record.end_reason = f"tests held for the change: {', '.join(sorted(held)) or '(none)'}"
        spec_record.finish(spec_policy.denials)
        record.finish(build_policy.denials + spec_policy.denials)

    minutes, seconds = divmod(int(record.seconds), 60)
    print(
        f"\n\033[1mDone in {minutes}m {seconds}s\033[0m - ${record.cost_usd:.4f} "
        f"(spec-tester ${spec_record.cost_usd:.4f})"
    )
    for url in record.pr_urls:
        print(f"  PR: {url}")
    print(f"  Report: {run_dir / 'report.md'}  (spec-tester: {run_dir / 'spec' / 'report.md'})")
    print(f"\033[1mEnded because:\033[0m {record.end_reason or '(not recorded)'}")
    return exit_code


def collect_acceptance(source: Path, dest: Path) -> list[str]:
    """Copy what the session wrote (not the harness's base class) into the run record."""
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for f in sorted(source.glob("*")):
        if f.is_file() and f.name != ACCEPTANCE_BASE.name:
            shutil.copy(f, dest / f.name)
            written.append(f.name)
    return written


class SessionOutcome:
    def __init__(self) -> None:
        self.is_error = False
        # Set when the SDK stopped the session rather than the agent finishing:
        # max_turns, a rate limit, an execution error. That run pushed nothing.
        self.stopped_by: str | None = None
        self.last_text = ""


async def run_session(
    options: ClaudeAgentOptions,
    message: str,
    record: RunRecord,
    status: StatusLine,
    interactive: bool,
) -> SessionOutcome:
    """One agent session: a first message, and follow-ups if interactive."""
    outcome = SessionOutcome()
    async with ClaudeSDKClient(options=options) as client:
        while True:
            turn = record.start_turn()
            await client.query(message)
            status.start()

            try:
                async for msg in client.receive_response():
                    record.append_message(msg)

                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock):
                                record.note_tool(block.name)
                            if isinstance(block, TextBlock):
                                record.note_text(block.text)
                                outcome.last_text = block.text
                            rendered = render_block(block)
                            if rendered:
                                status.write(rendered)
                    elif isinstance(msg, ResultMessage):
                        # Recorded so a run can be resumed or forked later.
                        # Note this is necessary but not sufficient: the CLI
                        # keeps session transcripts under ~/.claude, which
                        # `docker run --rm` discards, and keys them by working
                        # directory, which is unique per run. See README.
                        if msg.session_id:
                            record.metadata.setdefault("session_id", msg.session_id)
                        record.sdk_turns += msg.num_turns
                        record.cost_usd += msg.total_cost_usd or 0.0
                        subtype = getattr(msg, "subtype", "") or ""
                        if subtype.startswith("error"):
                            outcome.stopped_by = subtype
                        if msg.is_error:
                            record.errors.append(msg.result or subtype or "unknown error")
                            outcome.is_error = True
                            outcome.stopped_by = outcome.stopped_by or (msg.result or "error")[:200]
                        if msg.result:
                            record.note_text(msg.result)
                    elif isinstance(msg, SystemMessage) and msg.subtype == "error":
                        record.errors.append(str(msg.data))
            finally:
                await status.stop()

            record.end_turn()
            print(
                f"\033[2m  exchange {turn.index}: {turn.seconds:.0f}s, "
                f"${record.cost_usd:.4f} so far\033[0m"
            )
            print("\033[1m" + "─" * 72 + "\033[0m")

            if not interactive:
                break

            followup = read_followup()
            if followup is None:
                break
            message = followup
    return outcome


def _snapshot(record: RunRecord) -> tuple[float, float, int]:
    return (time.time(), record.cost_usd, record.sdk_turns)


def _delta(record: RunRecord, before: tuple[float, float, int]) -> dict:
    t0, cost0, turns0 = before
    return {
        "agent_seconds": round(time.time() - t0, 1),
        "cost_usd": round(record.cost_usd - cost0, 4),
        "sdk_turns": record.sdk_turns - turns0,
    }


async def fix_forward(
    args: argparse.Namespace,
    pr: ck.PullRequest,
    workspace: ws.Workspace,
    runbook: str,
    make_options,  # noqa: ANN001
    record: RunRecord,
    status: StatusLine,
    note: str = "",
) -> int:
    """Wait for the PR's checks; on red, hand the failure to a fresh session.

    Returns an exit code and sets record.end_reason. Never merges: who merges on
    green is OQ-5, and today the answer is a human.
    """
    previous_sha = None
    for round_no in range(0, args.fix_rounds + 1):
        head = ck.pr_head(pr)
        sha, branch, base = head["headRefOid"], head["headRefName"], head["baseRefName"]
        if round_no > 0 and sha == previous_sha:
            record.end_reason = f"fix round {round_no} ended without pushing a new commit"
            return 1
        previous_sha = sha

        print(f"\n\033[1mWaiting for checks on {pr.url} @ {sha[:7]}\033[0m")

        def show(v: ck.Verdict, elapsed: float) -> None:
            waiting = ", ".join(v.waiting_for) if v.waiting_for else ""
            print(f"\033[2m  {int(elapsed):>4}s  {v.state}  {waiting}\033[0m")

        v, waited = ck.wait_for_checks(
            lambda: ck.check_runs(pr, sha), timeout=args.checks_timeout, on_poll=show
        )
        record.rounds[-1].update(
            {
                "sha": sha[:7],
                "checks": v.state,
                "checks_seconds": round(waited, 1),
                "failed_checks": [c.name for c in v.failed],
            }
        )

        if v.state == ck.GREEN:
            fixes = round_no
            record.end_reason = (
                "checks green on first push" if fixes == 0
                else f"checks green after {fixes} fix round{'s' if fixes > 1 else ''} - ready for a human to merge"
            )
            return 0
        if v.state == ck.PENDING:
            record.end_reason = f"checks did not finish within {args.checks_timeout}s ({', '.join(v.waiting_for)})"
            return 1
        if round_no == args.fix_rounds:
            record.end_reason = (
                f"checks still red after {args.fix_rounds} fix rounds "
                f"({', '.join(c.name for c in v.failed)}) - the agent's last explanation is in the transcript"
            )
            return 1

        failures = []
        for check in v.failed:
            try:
                log = ck.trim_log(ck.job_log(pr, check))
            except ck.ChecksError as exc:
                log = f"(the harness could not fetch this log: {exc})"
            failures.append((check.name, log))
        (record.directory / f"round-{round_no + 1}-failure.log").write_text(
            "\n\n".join(f"== {n}\n{log}" for n, log in failures), encoding="utf-8"
        )

        checkout_pr_branch(workspace.app, branch, sha)
        brief = ck.fix_brief(
            round_no=round_no + 1,
            max_rounds=args.fix_rounds,
            request=args.request,
            pr_url=pr.url,
            branch=branch,
            diff_stat=diff_stat(workspace.app, base),
            failures=failures,
            runbook=runbook,
            note=note,
        )
        print(f"\n\033[1mFix round {round_no + 1}: {', '.join(n for n, _ in failures)} failed\033[0m")
        print("\033[1m" + "─" * 72 + "\033[0m")

        before = _snapshot(record)
        # A fresh session: new client, new context. Only the brief carries over.
        outcome = await run_session(make_options(fix=True), brief, record, status, interactive=False)
        record.rounds.append({"round": round_no + 1, "kind": "fix", **_delta(record, before)})
        if outcome.stopped_by:
            record.end_reason = f"fix round {round_no + 1} was stopped ({outcome.stopped_by})"
            return 1
    return 1  # unreachable: the loop returns on every path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AaaS agent for one task.")
    parser.add_argument("--task", choices=sorted(TASKS), default="create-deployment")
    parser.add_argument(
        "--request",
        required=True,
        help="The request, or @path to read it from a file.",
    )
    parser.add_argument(
        "--app-repo",
        default=None,
        help="Application repository for --task create-app, e.g. aaas-app-demo. It must "
        "already exist; creating it is provisioning, not agent work.",
    )
    parser.add_argument(
        "--change",
        default=None,
        help="create-change only: a short name for the change, e.g. item-archive. The "
        "change id is today's date plus this. Defaults to the brief's file name.",
    )
    parser.add_argument("--owner", default=os.environ.get("AAAS_GITHUB_OWNER", "main0034"))
    parser.add_argument("--runs-dir", default=os.environ.get("AAAS_RUNS_DIR", "runs"))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--model", default=os.environ.get("AAAS_MODEL") or None)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument(
        "--fix-rounds",
        type=int,
        default=0,
        help="create-app only: after the PR is opened, wait for its checks and, if one "
        "fails, give a fresh session the failure log to fix it on the same branch - up "
        "to this many times. The runbook allows two. Implies --non-interactive.",
    )
    parser.add_argument(
        "--skip-local-checks",
        action="store_true",
        help="create-app only: tell the initial session to push without the runbook's "
        "local build/test/format, so CI meets the code first. For producing a red check "
        "on purpose (Phase 5). Fix rounds are never told this.",
    )
    parser.add_argument(
        "--checks-timeout",
        type=int,
        default=25 * 60,
        help="Seconds to wait for a commit's checks before giving up.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt for follow-up input. The agent gets exactly one turn to "
        "finish, so anything it would have asked about must be in the request.",
    )
    args = parser.parse_args()
    if args.skip_local_checks and args.task != "create-app":
        parser.error("--skip-local-checks only applies to --task create-app")
    if args.task == "create-change":
        # Always waits for checks; --fix-rounds 0 reports the verdict without fixing.
        args.non_interactive = True
        if args.change is None and args.request.startswith("@"):
            args.change = Path(args.request[1:]).stem
        if not args.change:
            parser.error("--task create-change needs --change <name> when the request is not @file")
        if not 0 <= args.fix_rounds <= 2:
            parser.error("--fix-rounds must be 0, 1 or 2 - create-app.md section 6 allows two")
    elif args.fix_rounds:
        if args.task != "create-app":
            parser.error("--fix-rounds is only implemented for --task create-app and create-change")
        if not 0 < args.fix_rounds <= 2:
            parser.error("--fix-rounds must be 1 or 2 - create-app.md section 6 allows two")
        args.non_interactive = True

    if args.request.startswith("@"):
        brief = Path(args.request[1:])
        if not brief.is_file():
            # This runs inside the container, where only a couple of host
            # directories are mounted. A path that exists on the laptop is not
            # automatically a path that exists here, and "No such file or
            # directory" does not hint at that at all.
            here = Path("briefs")
            available = sorted(p.as_posix() for p in here.glob("*.md")) if here.is_dir() else []
            print(
                f"No such brief inside the container: {brief}\n"
                f"run.sh mounts ./briefs (read-only) and ./runs, and nothing else - "
                f"a file kept anywhere else on the host is not visible in here.\n"
                + (
                    "Available briefs: " + ", ".join(available)
                    if available
                    else "briefs/ is empty or not mounted."
                ),
                file=sys.stderr,
            )
            return 2
        args.request = brief.read_text(encoding="utf-8")

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
