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
import sys
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

from . import workspace as ws
from .policy import ToolPolicy, make_pre_tool_use_hook
from .report import RunRecord

ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite"]
DISALLOWED_TOOLS = ["WebFetch", "WebSearch", "Task", "NotebookEdit"]

TASKS = {
    "create-deployment": "agent/create-deployment.md",
    "create-app": "agent/create-app.md",
}


def briefing(task: str, workspace: ws.Workspace, runbook: str) -> str:
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


def render_block(block) -> str:  # noqa: ANN001
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, ThinkingBlock):
        return ""
    if isinstance(block, ToolUseBlock):
        detail = block.input.get("command") or block.input.get("file_path") or block.input.get("pattern") or ""
        return f"  \033[2m→ {block.name}({str(detail)[:110]})\033[0m"
    return ""


async def run(args: argparse.Namespace) -> int:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.runs_dir) / run_id
    print(f"\033[1mRun {run_id}\033[0m  ({run_dir})\n")

    print("Preparing workspace…")
    try:
        workspace = ws.prepare(run_dir / "workspace", owner=args.owner)
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

    prompt_path = workspace.deployments / "agent" / "PROMPT.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")

    # /tmp is writable because the runbooks tell the agent to compose a PR body
    # in a file - a heredoc inside an argument is a reliable way to produce a
    # confusing shell error, which is why release.yml does the same thing.
    policy = ToolPolicy(writable_roots=[*workspace.writable_roots, Path("/tmp")])

    options = ClaudeAgentOptions(
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt + "\n\n" + briefing(args.task, workspace, runbook),
        },
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        permission_mode="bypassPermissions",
        hooks={
            "PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_hook(policy)])],
        },
        cwd=str(workspace.deployments),
        add_dirs=[str(workspace.reference)],
        max_turns=args.max_turns,
        model=args.model,
        setting_sources=None,
        env={"GH_TOKEN": os.environ.get("GH_TOKEN", "")},
    )

    record = RunRecord(
        run_id=run_id,
        directory=run_dir,
        task=args.task,
        request=args.request,
        metadata={
            "deployments HEAD": ws.head_sha(workspace.deployments),
            "runbook": runbook,
            "model": args.model or "(CLI default)",
            "auth": "subscription" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api-key",
        },
    )

    print(f"Workspace ready at {workspace.deployments}\n")
    print("\033[1m" + "─" * 72 + "\033[0m")

    message = args.request
    exit_code = 0

    async with ClaudeSDKClient(options=options) as client:
        while True:
            record.start_turn()
            await client.query(message)

            async for msg in client.receive_response():
                record.append_message(msg)

                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            record.note_tool(block.name)
                        if isinstance(block, TextBlock):
                            record.note_text(block.text)
                        rendered = render_block(block)
                        if rendered:
                            print(rendered)
                elif isinstance(msg, ResultMessage):
                    record.sdk_turns += msg.num_turns
                    record.cost_usd += msg.total_cost_usd or 0.0
                    if msg.is_error:
                        record.errors.append(msg.result or "unknown error")
                        exit_code = 1
                    if msg.result:
                        record.note_text(msg.result)
                elif isinstance(msg, SystemMessage) and msg.subtype == "error":
                    record.errors.append(str(msg.data))

            record.end_turn()
            print("\033[1m" + "─" * 72 + "\033[0m")

            if args.non_interactive:
                break

            try:
                message = input("\n\033[1mYou\033[0m (blank to finish): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not message:
                break
            print()

    record.finish(policy.denials)

    minutes, seconds = divmod(int(record.seconds), 60)
    print(f"\n\033[1mDone in {minutes}m {seconds}s\033[0m — ${record.cost_usd:.4f}, {len(record.turns)} turns")
    if record.pr_urls:
        for url in record.pr_urls:
            print(f"  PR: {url}")
    else:
        print("  \033[33mNo pull request URL appeared in the output.\033[0m")
        exit_code = exit_code or 1
    if policy.denials:
        print(f"  {len(policy.denials)} policy refusal(s) — see report.md")
    print(f"  Report: {run_dir / 'report.md'}")

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AaaS agent for one task.")
    parser.add_argument("--task", choices=sorted(TASKS), default="create-deployment")
    parser.add_argument(
        "--request",
        required=True,
        help="The request, or @path to read it from a file.",
    )
    parser.add_argument("--owner", default=os.environ.get("AAAS_GITHUB_OWNER", "main0034"))
    parser.add_argument("--runs-dir", default=os.environ.get("AAAS_RUNS_DIR", "runs"))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--model", default=os.environ.get("AAAS_MODEL") or None)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt for follow-up input. The agent gets exactly one turn to "
        "finish, so anything it would have asked about must be in the request.",
    )
    args = parser.parse_args()

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
