"""
Tool policy for the AaaS agent.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This module is a *guidance* layer. It stops the agent from drifting into
things it should not be doing, and it produces a legible refusal that names
the remedy instead of a confusing downstream failure.

It is NOT a security boundary, and it must never be described as one.

The agent is allowed to run `python3` (it has to: `scripts/validate_deployment.py`
is the schema gate it is required to run before pushing). Anything that can run
Python can run `pip install azure-cli`. An allowlist of command names cannot
survive that, and pretending otherwise is how you end up trusting the wrong
layer.

The actual containment is two things, in this order:

  1. There is no Azure credential anywhere the agent can reach. No service
     principal secret, no ~/.azure, no ARM_* or AZURE_* variables in the
     container. This is the property the whole pipeline design rests on
     (FINDINGS.md #11) and it is the one that holds even if every rule below
     is bypassed.
  2. The container has no `az` and no `terraform` binary, and no path to the
     state backend. `verify-isolation` asserts this at start-up rather than
     assuming it.

So: rules below = fewer wasted turns and clearer failures. Container +
credential absence = the boundary. Keep the two ideas apart.

`dotnet` makes this more true, not less. The create-app runbook requires
`dotnet build` and `dotnet test`, and both execute arbitrary code by design:
MSBuild targets run at build time, and a test is a program the agent wrote.
There is no subcommand list that narrows that. What the agent can reach from
inside that code is exactly what the container gives it - no Azure credential,
no `az`, network egress (NuGet needs it), and `GH_TOKEN`. The subcommand rules
below keep the agent on the runbook's path; the blast radius is set by the
token's permissions, which GitHub enforces (FINDINGS.md #19, point 5).

The narrowing that *is* worth having: `python3` is restricted to running the
validation script. That closes the widest hole for the price of one regex,
and jq covers everything else the runbook actually asks for.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Bash
# --------------------------------------------------------------------------

# Read-only inspection and the tools the runbooks actually name.
ALLOWED_COMMANDS: set[str] = {
    "git",
    "gh",
    "jq",
    "python3",
    "ls",
    "cat",
    "pwd",
    "cd",
    "echo",
    "printf",
    "grep",
    "rg",
    "find",
    "head",
    "tail",
    "wc",
    "diff",
    "sort",
    "uniq",
    "cut",
    "tr",
    "basename",
    "dirname",
    "mkdir",
    "cp",
    "mv",
    "test",
    "true",
    "false",
    "env",
    "which",
    "command",
    "dotnet",
}

# Named explicitly so the refusal can say *why*, rather than "not in allowlist".
DENIED_COMMANDS: dict[str, str] = {
    "terraform": (
        "The agent never runs Terraform. Your only path to production is a pull "
        "request that CI plans and applies. Open the PR instead."
    ),
    "az": (
        "The agent never talks to Azure. There is no credential in this container "
        "and no CLI to use one. If you need to know what a plan would do, that is "
        "what the plan comment on the PR is for."
    ),
    "terragrunt": "Same rule as terraform: no direct infrastructure execution.",
    "docker": (
        "Images are built by app CI on merge, tagged with the git SHA. Building "
        "locally would produce an image the deployments repo cannot reference."
    ),
    "kubectl": "Not part of this stack.",
    "pip": (
        "Dependencies are fixed at container build time so a run is reproducible. "
        "If something is genuinely missing, stop and say so."
    ),
    "pip3": "See pip.",
    "npm": "See pip.",
    "curl": (
        "No ad-hoc network calls. Use `gh` for anything GitHub, and read files "
        "from the checkout for anything else."
    ),
    "wget": "See curl.",
    "nc": "See curl.",
    "ssh": "See curl.",
    "scp": "See curl.",
    "sudo": "There is nothing here worth escalating to.",
    "apt": "The container image is fixed.",
    "apt-get": "The container image is fixed.",
    "chmod": "Nothing in a deployment directory needs an execute bit.",
    "tee": "Use the Write tool; shell redirection bypasses the path guard.",
    "dd": "Use the Write tool.",
    "truncate": "Use the Write tool.",
    "sed": (
        "Use the Read and Edit tools rather than in-place stream editing - Edit "
        "goes through the same path guard and shows up as a reviewable diff."
    ),
    "awk": "Use jq for JSON and the Read tool for everything else.",
    "rm": (
        "Nothing in this workflow requires deleting files. If a file should not "
        "exist, say so and stop."
    ),
    "rmdir": "See rm.",
}

# `gh` is broad enough to need its own allowlist - it can create and delete
# repositories, change settings, and call arbitrary REST endpoints.
ALLOWED_GH_SUBCOMMANDS: set[str] = {"pr", "repo", "run", "auth", "browse", "search"}
DENIED_GH_SUBCOMMANDS: dict[str, str] = {
    "api": (
        "Raw API calls are outside the runbook. Use the specific `gh pr` / `gh run` "
        "commands the runbook names."
    ),
    "secret": "Repository secrets are provisioned by a human, never by the agent.",
    "variable": "Repository variables are provisioned by a human, never by the agent.",
    "ruleset": "That would be the agent editing its own guardrails.",
    "workflow": "Workflows are guardrails. They are not yours to run or edit.",
    "release": "Releases are produced by CI.",
    "ssh-key": "No.",
    "gpg-key": "No.",
}

# `gh repo` can create and delete. Only reading and cloning are in scope for
# the deployment task; repo creation is a provisioning step the harness does.
ALLOWED_GH_REPO_SUBCOMMANDS: set[str] = {"view", "clone", "list"}

# dotnet: the subcommands the create-app runbook names, and read-only ones.
# Everything else is refused with a reason. `new` is refused outright: the app
# repository already exists and is created from the template by provisioning.
ALLOWED_DOTNET_SUBCOMMANDS: set[str] = {
    "build",
    "test",
    "format",
    "restore",
    "clean",
    "--info",
    "--version",
    "--list-sdks",
}
DENIED_DOTNET_SUBCOMMANDS: dict[str, str] = {
    "new": (
        "The application repository already exists and was created from the "
        "template. Add files with the Write tool."
    ),
    "nuget": (
        "Package sources and signing are fixed by the repository. Packages come "
        "from nuget.org through `dotnet restore`."
    ),
    "run": (
        "Running the app needs the database, which you do not have. Prove behaviour "
        "with `dotnet test`; CI smoke-tests the container."
    ),
    "publish": "Images are built by CI on merge. There is nothing to publish here.",
    "pack": "Nothing in this repository is a package.",
    "sln": "The solution structure is part of the template.",
    "workload": "The SDK is fixed at image build time.",
    "dev-certs": "Not needed: TLS terminates at the platform.",
    "user-secrets": "There are no secrets in this application, by design (D-14).",
}
# `dotnet tool` only to restore the pinned tool manifest (dotnet-ef).
ALLOWED_DOTNET_TOOL_SUBCOMMANDS: set[str] = {"restore", "list"}
# `dotnet ef`: migrations only, and never against a database.
ALLOWED_DOTNET_EF_MIGRATIONS: set[str] = {
    "add",
    "remove",
    "list",
    "script",
    "has-pending-model-changes",
}

# python3 exists solely to run the schema validator.
VALIDATOR_RE = re.compile(r"(^|/)scripts/validate_deployment\.py$")

SUBSTITUTION_RE = re.compile(r"\$\(|`|<\(|>\(")
REDIRECT_RE = re.compile(r"(?<![0-9<>])>{1,2}(?!&)")

SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")

ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# Guardrail paths inside an application checkout. Mirrors the "Do not edit these"
# table in the template's AGENT.md.
APP_PROTECTED_GLOBS: tuple[str, ...] = (
    "Dockerfile",
    ".github/",
    "scripts/",
    "Directory.Build.props",
    "global.json",
    ".editorconfig",
    ".aaas/",
    "AGENT.md",
    "dotnet-tools.json",
)

DEPLOYMENTS_PROTECTED_GLOBS: tuple[str, ...] = (
    "schemas/",
    "scripts/",
    ".github/",
    "agent/",
    "AGENT.md",
)


@dataclass
class Decision:
    allow: bool
    reason: str = ""


@dataclass
class Denial:
    tool: str
    detail: str
    reason: str


@dataclass
class ToolPolicy:
    """Evaluates a tool call against the rules above.

    `writable_roots` is the set of directories the agent may create or modify
    files in. For the create-deployment task that is exactly one directory:
    the deployments tree. Everything else in the checkout - schemas, scripts,
    workflows, the runbooks themselves - is readable and not writable, which
    mirrors CODEOWNERS and the `guardrails` CI job, but fails in one second
    instead of on a PR check.
    """

    writable_roots: list[Path]
    protected_globs: tuple[str, ...] = DEPLOYMENTS_PROTECTED_GLOBS
    denials: list[Denial] = field(default_factory=list)

    # -- entry point -------------------------------------------------------

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> Decision:
        if tool_name == "Bash":
            decision = self._check_bash(str(tool_input.get("command", "")))
            detail = str(tool_input.get("command", ""))[:200]
        elif tool_name in {"Write", "Edit", "NotebookEdit", "MultiEdit"}:
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            decision = self._check_write(str(path))
            detail = str(path)
        else:
            return Decision(allow=True)

        if not decision.allow:
            self.denials.append(Denial(tool=tool_name, detail=detail, reason=decision.reason))
        return decision

    # -- bash --------------------------------------------------------------

    def _check_bash(self, command: str) -> Decision:
        if not command.strip():
            return Decision(False, "Empty command.")

        if SUBSTITUTION_RE.search(command):
            return Decision(
                False,
                "Command substitution ($(...), backticks, process substitution) is not "
                "allowed, because it hides the command actually being run from this "
                "check. Run the steps separately.",
            )

        if REDIRECT_RE.search(command):
            return Decision(
                False,
                "Shell redirection is not allowed - it writes files without going "
                "through the path guard. Use the Write tool to create a file, or the "
                "Edit tool to change one.",
            )

        for segment in SEGMENT_SPLIT_RE.split(command):
            segment = segment.strip()
            if not segment:
                continue
            decision = self._check_segment(segment)
            if not decision.allow:
                return decision

        return Decision(True)

    def _check_segment(self, segment: str) -> Decision:
        try:
            tokens = shlex.split(segment)
        except ValueError as exc:
            return Decision(False, f"Could not parse the command ({exc}). Simplify it.")

        while tokens and ASSIGNMENT_RE.match(tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            return Decision(True)

        program = Path(tokens[0]).name

        if program in DENIED_COMMANDS:
            return Decision(False, f"`{program}` is not available. {DENIED_COMMANDS[program]}")

        if program not in ALLOWED_COMMANDS:
            return Decision(
                False,
                f"`{program}` is not one of the commands available here. The runbooks "
                f"use git, gh, jq, dotnet and python3; if you need something else, stop "
                f"and say what and why.",
            )

        if program == "python3":
            return self._check_python(tokens)
        if program == "gh":
            return self._check_gh(tokens)
        if program == "git":
            return self._check_git(tokens)
        if program == "dotnet":
            return self._check_dotnet(tokens)

        return Decision(True)

    def _check_python(self, tokens: list[str]) -> Decision:
        args = [t for t in tokens[1:] if not t.startswith("-")]
        if not args:
            return Decision(
                False,
                "python3 is available only to run `scripts/validate_deployment.py`. "
                "An interactive interpreter is not.",
            )
        if not VALIDATOR_RE.search(args[0]):
            return Decision(
                False,
                "python3 is available only to run `scripts/validate_deployment.py` - "
                "the schema gate you are required to pass before pushing. Use jq for "
                "JSON and the Read tool for files.",
            )
        if any(t in {"-c", "-m"} for t in tokens):
            return Decision(False, "python3 -c / -m is not available.")
        return Decision(True)

    def _check_gh(self, tokens: list[str]) -> Decision:
        args = [t for t in tokens[1:] if not t.startswith("-")]
        if not args:
            return Decision(True)
        sub = args[0]
        if sub in DENIED_GH_SUBCOMMANDS:
            return Decision(False, f"`gh {sub}` is not available. {DENIED_GH_SUBCOMMANDS[sub]}")
        if sub not in ALLOWED_GH_SUBCOMMANDS:
            return Decision(False, f"`gh {sub}` is outside the runbook.")
        if sub == "repo":
            if len(args) > 1 and args[1] not in ALLOWED_GH_REPO_SUBCOMMANDS:
                return Decision(
                    False,
                    f"`gh repo {args[1]}` is not available. Repository creation and "
                    f"deletion are provisioning steps, done by the harness or a human.",
                )
        if sub == "pr":
            if len(args) > 1 and args[1] in {"merge", "close", "review"}:
                return Decision(
                    False,
                    "You never merge, close or approve your own pull request. Open it, "
                    "report the URL, and stop.",
                )
        return Decision(True)

    def _check_dotnet(self, tokens: list[str]) -> Decision:
        if len(tokens) < 2:
            return Decision(False, "Say which dotnet command: build, test, format, restore.")
        sub = tokens[1]
        if sub in DENIED_DOTNET_SUBCOMMANDS:
            return Decision(False, f"`dotnet {sub}` is not available. {DENIED_DOTNET_SUBCOMMANDS[sub]}")
        if sub in ALLOWED_DOTNET_SUBCOMMANDS:
            return Decision(True)
        if sub == "tool":
            action = tokens[2] if len(tokens) > 2 else ""
            if action in ALLOWED_DOTNET_TOOL_SUBCOMMANDS:
                return Decision(True)
            return Decision(
                False,
                f"`dotnet tool {action}` is not available. The tools this repository "
                f"uses are pinned in dotnet-tools.json; run `dotnet tool restore`.",
            )
        if sub == "ef":
            area = tokens[2] if len(tokens) > 2 else ""
            action = tokens[3] if len(tokens) > 3 else ""
            if area == "migrations" and action in ALLOWED_DOTNET_EF_MIGRATIONS:
                return Decision(True)
            if area == "database":
                return Decision(
                    False,
                    "Migrations are applied by the platform's `migrate` init container "
                    "before the new version starts, and by CI against a throwaway "
                    "Postgres. You have no database, and never apply one yourself.",
                )
            return Decision(
                False,
                "`dotnet ef` is available for `migrations add|remove|list|script|"
                "has-pending-model-changes` only.",
            )
        if sub == "add":
            # `dotnet add [<project>] package <name> --version <v>`. Versions are
            # pinned centrally; an unpinned add would float.
            if "package" in tokens and "--version" in tokens:
                return Decision(True)
            if "package" in tokens:
                return Decision(
                    False,
                    "Pin the version: `dotnet add <project> package <name> --version <v>`. "
                    "Versions live in Directory.Packages.props.",
                )
            return Decision(False, "Project references are part of the template's structure.")
        return Decision(
            False,
            f"`dotnet {sub}` is outside the runbook. The runbook uses restore, build, "
            f"test, format, `tool restore` and `ef migrations`.",
        )

    def _check_git(self, tokens: list[str]) -> Decision:
        args = [t for t in tokens[1:] if not t.startswith("-")]
        sub = args[0] if args else ""
        if sub == "push":
            if any(t in {"--force", "-f", "--force-with-lease"} for t in tokens):
                return Decision(
                    False,
                    "Force-pushing rewrites history CI has already reported on. Push a "
                    "new commit instead.",
                )
            if any(t in {"master", "main", "origin/master", "origin/main"} for t in args[1:]):
                return Decision(
                    False,
                    "Never push to the default branch. Push your branch and open a PR.",
                )
        if sub in {"config"} and "--global" in tokens:
            return Decision(False, "Global git config is set by the harness, not the agent.")
        return Decision(True)

    # -- writes ------------------------------------------------------------

    def _check_write(self, raw_path: str) -> Decision:
        if not raw_path:
            return Decision(False, "No file path given.")

        path = Path(raw_path)
        if not path.is_absolute():
            return Decision(
                False,
                "Use an absolute path so the write can be checked against the "
                "directories you are allowed to change.",
            )
        path = Path(str(path))

        for root in self.writable_roots:
            try:
                rel = path.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            rel_str = str(rel)
            for protected in self.protected_globs:
                if rel_str == protected.rstrip("/") or rel_str.startswith(protected):
                    return Decision(
                        False,
                        f"`{rel_str}` is a guardrail file. Guardrails are changed by a "
                        f"human, in a separate pull request, never by the change they "
                        f"would have blocked. If you think this file is wrong, say so "
                        f"and stop.",
                    )
            return Decision(True)

        allowed = ", ".join(str(r) for r in self.writable_roots)
        return Decision(
            False,
            f"`{path}` is outside the directories you may write to ({allowed}). "
            f"Reference material is readable but not editable.",
        )


def make_pre_tool_use_hook(policy: ToolPolicy):
    """Wrap the policy as a PreToolUse hook.

    Returning `deny` puts the reason back in front of the model as a tool
    result, so a refusal reads as an instruction to try something else rather
    than as an unexplained failure. That matters: the runbook asks the agent to
    fix forward, and it can only do that if the refusal names the remedy.
    """

    async def hook(input_data, tool_use_id, context):  # noqa: ANN001 - SDK types
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
        decision = policy.check(tool_name, tool_input)
        if decision.allow:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }

    return hook
