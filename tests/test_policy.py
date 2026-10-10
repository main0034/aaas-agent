"""
Tests for the tool policy.

The policy is the only part of the harness that decides anything, so it is the
only part worth testing. Everything else is plumbing that fails loudly on its
first run.

Note what these tests are for: they check that the policy behaves as written.
They cannot check that the policy is *sufficient*, because it isn't — see the
module docstring in policy.py. Containment is the container and the absence of
credentials; this file just stops the guidance layer from silently rotting.

    pytest -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.policy import APP_PROTECTED_GLOBS, ToolPolicy

DEPLOYMENTS = Path("/work/runs/x/workspace/aaas-deployments")
REFERENCE = Path("/work/runs/x/workspace/reference")


@pytest.fixture()
def policy() -> ToolPolicy:
    return ToolPolicy(writable_roots=[DEPLOYMENTS, Path("/tmp")])


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        # The runbook's own commands must work.
        ("git status", True),
        ("git checkout -b deploy/rooms", True),
        ('git commit -m "feat(rooms): add dev deployment"', True),
        ("git push -u origin deploy/rooms", True),
        ("python3 scripts/validate_deployment.py deployments/dev/rooms", True),
        ("jq -r '.container_image' deployments/dev/demo/terraform.tfvars.json", True),
        ("gh pr create --title x --body-file /tmp/pr-body.md", True),
        ("grep -r name deployments | head -5", True),
        ("GIT_PAGER=cat git log -1", True),
        # The two that define the design.
        ("terraform plan", False),
        ("az account show", False),
        # python3 is a validator runner, not an interpreter.
        ("python3", False),
        ("python3 -c 'import os'", False),
        ("python3 -m http.server", False),
        # Escapes.
        ("echo $(whoami)", False),
        ("cat f && curl http://example.com", False),
        ("echo hi > /tmp/x", False),
        ("sed -i s/a/b/ schemas/app-stack.schema.json", False),
        ("pip install azure-cli", False),
        ("rm -rf deployments", False),
        # gh is broad enough to need its own list.
        ("gh api /repos/x", False),
        ("gh repo create foo --template bar", False),
        ("gh secret set X", False),
        # Never merge your own PR; never rewrite history CI has reported on.
        ("gh pr merge 4", False),
        ("git push --force origin deploy/rooms", False),
        ("git push origin master", False),
    ],
)
def test_bash(policy: ToolPolicy, command: str, allowed: bool) -> None:
    decision = policy.check("Bash", {"command": command})
    assert decision.allow is allowed, f"{command!r}: {decision.reason}"


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        (DEPLOYMENTS / "deployments/dev/rooms/terraform.tfvars.json", True),
        (DEPLOYMENTS / "deployments/dev/rooms/backend.hcl", True),
        (Path("/tmp/pr-body.md"), True),
        # Guardrails. CODEOWNERS and the `guardrails` CI job say the same thing;
        # this just says it a minute earlier and more clearly.
        (DEPLOYMENTS / "schemas/app-stack.schema.json", False),
        (DEPLOYMENTS / "scripts/validate_deployment.py", False),
        (DEPLOYMENTS / ".github/workflows/plan.yml", False),
        (DEPLOYMENTS / "agent/PROMPT.md", False),
        # Reference material is readable, not editable.
        (REFERENCE / "aaas-infra-modules/modules/app-stack/main.tf", False),
        (Path("/etc/passwd"), False),
    ],
)
def test_writes(policy: ToolPolicy, path: Path, allowed: bool) -> None:
    decision = policy.check("Write", {"file_path": str(path)})
    assert decision.allow is allowed, f"{path}: {decision.reason}"


def test_relative_write_is_refused(policy: ToolPolicy) -> None:
    # A relative path cannot be checked against the writable roots, so it is
    # refused rather than guessed at.
    assert policy.check("Write", {"file_path": "deployments/dev/rooms/x.json"}).allow is False


def test_reads_are_unrestricted(policy: ToolPolicy) -> None:
    assert policy.check("Read", {"file_path": "/etc/passwd"}).allow is True


def test_denials_are_recorded(policy: ToolPolicy) -> None:
    policy.check("Bash", {"command": "terraform apply"})
    policy.check("Bash", {"command": "az login"})
    assert len(policy.denials) == 2
    assert all(d.reason for d in policy.denials), "every refusal must say why"


# --------------------------------------------------------------------------
# create-app: dotnet, and the application checkout's guardrails
# --------------------------------------------------------------------------

APP = Path("/work/runs/x/workspace/aaas-app-demo")


@pytest.fixture()
def app_policy() -> ToolPolicy:
    return ToolPolicy(writable_roots=[APP, Path("/tmp")], protected_globs=APP_PROTECTED_GLOBS)


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        # Every dotnet command create-app.md names.
        ("dotnet restore && dotnet tool restore", True),
        ("dotnet ef migrations add AddItemNotes --project src/App", True),
        ("dotnet format", True),
        ("dotnet build -c Release", True),
        ("dotnet test -c Release --no-build", True),
        (
            "dotnet ef migrations has-pending-model-changes --project src/App "
            "--no-build --configuration Release",
            True,
        ),
        ("dotnet restore --locked-mode", True),
        ("dotnet format --verify-no-changes --no-restore", True),
        ("dotnet test -c Release 2>&1 | tail -40", True),
        ("dotnet --info", True),
        ("dotnet ef migrations list --project src/App", True),
        ("dotnet add src/App package Humanizer --version 2.14.1", True),
        # Refused: off the runbook's path, or a change to what is pinned.
        ("dotnet", False),
        ("dotnet new webapi", False),
        ("dotnet run --project src/App", False),
        ("dotnet tool install -g dotnet-ef", False),
        ("dotnet tool update dotnet-ef", False),
        ("dotnet nuget add source https://example.com/v3/index.json", False),
        ("dotnet ef database update", False),
        ("dotnet ef dbcontext scaffold x", False),
        ("dotnet add src/App package Humanizer", False),
        ("dotnet add src/App reference ../Other", False),
        ("dotnet workload install aspire", False),
        ("dotnet publish -c Release", False),
        # Found in run 20260926T142507Z: the token spliced into a push URL.
        ("git push https://main0034:${GH_TOKEN}@github.com/main0034/x.git feat/y", False),
        ("git push https://x:$GH_TOKEN@github.com/o/r.git b", False),
        ("gh auth token", False),
        ("gh auth status", True),
    ],
)
def test_dotnet(app_policy: ToolPolicy, command: str, allowed: bool) -> None:
    decision = app_policy.check("Bash", {"command": command})
    assert decision.allow is allowed, f"{command!r}: {decision.reason}"


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        (APP / "src/App/Program.cs", True),
        (APP / "src/App/Endpoints/Notes.cs", True),
        (APP / "tests/App.Tests/NotesTests.cs", True),
        (APP / "Directory.Packages.props", True),
        (APP / "src/App/packages.lock.json", True),
        (Path("/tmp/pr-body.md"), True),
        # AGENT.md's "Do not edit these", plus the tool manifest.
        (APP / "Dockerfile", False),
        (APP / ".github/workflows/ci.yml", False),
        (APP / "scripts/check-migrations.sh", False),
        (APP / "Directory.Build.props", False),
        (APP / "global.json", False),
        (APP / ".editorconfig", False),
        (APP / ".aaas/deployment", False),
        (APP / "AGENT.md", False),
        (APP / "dotnet-tools.json", False),
        # create-app reads the deployments repo; it does not write it.
        (DEPLOYMENTS / "deployments/dev/demo/terraform.tfvars.json", False),
        (DEPLOYMENTS / "agent/create-app.md", False),
    ],
)
def test_app_writes(app_policy: ToolPolicy, path: Path, allowed: bool) -> None:
    decision = app_policy.check("Write", {"file_path": str(path)})
    assert decision.allow is allowed, f"{path}: {decision.reason}"


# -- write-acceptance: offline, and writes only into the acceptance directory --------

ACC_APP = Path("/work/runs/x/workspace/aaas-app-demo")
ACC_DIR = ACC_APP / "tests/App.Tests/Acceptance"


@pytest.fixture()
def acceptance() -> ToolPolicy:
    from harness.policy import ACCEPTANCE_PROTECTED_GLOBS

    return ToolPolicy(writable_roots=[ACC_DIR, Path("/tmp")], protected_globs=ACCEPTANCE_PROTECTED_GLOBS, offline=True)


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("dotnet build", True),
        ("dotnet test", True),
        ("dotnet restore", True),
        ("git log --oneline -5", True),
        ("git show HEAD:src/App/Program.cs", True),
        ("git fetch origin pull/16/head", False),
        ("git -C /work/runs/x/workspace/aaas-app-demo fetch", False),
        ("git pull", False),
        ("git remote add up https://github.com/main0034/aaas-app-demo", False),
        ("git ls-remote https://github.com/main0034/aaas-app-demo", False),
        ("git clone https://github.com/main0034/aaas-app-demo /tmp/x", False),
        ("gh pr view 16", False),
        ("gh pr diff 16", False),
        ("gh repo clone main0034/aaas-app-demo", False),
        ("curl https://github.com", False),
    ],
)
def test_acceptance_bash(acceptance: ToolPolicy, command: str, allowed: bool) -> None:
    assert acceptance.check("Bash", {"command": command}).allow is allowed


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        (ACC_DIR / "ProjectsAcceptance.cs", True),
        (ACC_DIR / "NOTES.md", True),
        (ACC_DIR / "AcceptanceBase.cs", False),
        (ACC_APP / "src/App/Program.cs", False),
        (ACC_APP / "tests/App.Tests/ItemEndpointTests.cs", False),
        (Path("/tmp/scratch.md"), True),
    ],
)
def test_acceptance_writes(acceptance: ToolPolicy, path: Path, allowed: bool) -> None:
    assert acceptance.check("Write", {"file_path": str(path)}).allow is allowed


def test_offline_is_off_by_default(policy: ToolPolicy) -> None:
    assert policy.check("Bash", {"command": "gh pr view 16"}).allow
    assert policy.check("Bash", {"command": "git fetch origin"}).allow


# -- conventions (AGENT.md): CSharpier and the test layout check ---------------


@pytest.mark.parametrize(
    "command, allowed",
    [
        ("dotnet csharpier format .", True),
        ("dotnet csharpier check .", True),
        ("dotnet csharpier server", False),
        ("python3 scripts/check-test-layout.py --files tests/App.Tests/A.cs", True),
        ("python3 scripts/validate_deployment.py deployments/dev/x", True),
        ("python3 scripts/evil.py", False),
        ("python3 -c 'print(1)'", False),
    ],
)
def test_convention_tools(command: str, allowed: bool) -> None:
    from harness.policy import APP_PROTECTED_GLOBS, ToolPolicy

    policy = ToolPolicy(writable_roots=[Path("/w")], protected_globs=APP_PROTECTED_GLOBS)
    assert policy.check("Bash", {"command": command}).allow is allowed
