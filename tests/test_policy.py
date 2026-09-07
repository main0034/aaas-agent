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

from harness.policy import ToolPolicy

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
