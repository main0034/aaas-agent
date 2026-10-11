import base64
import json
from pathlib import Path

from harness import checks as ck
from harness import deliver as dl

TF = "deployments/dev/demo/terraform.tfvars.json"
OLD = {"name": "demo", "location": "northeurope", "container_image": "ghcr.io/o/app:aaa"}
SHA = "b" * 40
NEW = {**OLD, "container_image": f"ghcr.io/o/app:{SHA}"}


def test_deployment_path_and_branch(tmp_path: Path):
    (tmp_path / ".aaas").mkdir()
    (tmp_path / ".aaas" / "deployment").write_text("# comment\n\ndeployment_path=deployments/dev/demo\n")
    path = dl.deployment_path(tmp_path)
    assert path == "deployments/dev/demo"
    assert dl.deploy_branch(path, "d627b3518351") == "deploy/demo-d627b35"


def test_image_only_accepts_exactly_an_image_bump():
    assert dl.image_only([TF], TF, OLD, NEW) == []


def test_image_only_refuses_anything_else():
    assert dl.image_only([TF, "deployments/dev/demo/main.tf"], TF, OLD, NEW)
    assert dl.image_only([TF], TF, OLD, {**NEW, "location": "swedencentral"})
    assert dl.image_only([TF], TF, OLD, OLD)
    assert dl.image_only([TF], TF, OLD, {**NEW, "min_replicas": 1})


def test_read_deploy_comment():
    ok = "### Deployed — `deployments/dev/demo`\n\n**URL:** https://ca-demo.example.io\n**Revision:** `r1`"
    assert dl.read_deploy_comment(ok) == ("ok", "https://ca-demo.example.io", "")
    bad = ("### Deploy failed — `deployments/dev/demo`\n\nTerraform applied, but...\n\n"
           "**Reason:** revision not running\n**Cause:** `[migrate] FAILED: 23505`\n**URL:** https://x.io\n")
    state, url, reason = dl.read_deploy_comment(bad)
    assert state == "failed" and url == "https://x.io"
    assert "revision not running" in reason and "23505" in reason
    assert dl.read_deploy_comment("Plan: 0 to add") is None


class FakeGitHub:
    """Answers the gh calls deliver() makes, from a script of states."""

    def __init__(self, *, pr_appears_after=1, gate="success", files=(TF,), after=NEW,
                 applied_after=2, comment=None):
        self.polls = 0
        self.pr_appears_after = pr_appears_after
        self.gate, self.files, self.after = gate, list(files), after
        self.applied_after, self.comment = applied_after, comment
        self.merged = []
        self.applied_polls = 0

    def __call__(self, args):
        a = " ".join(args)
        if a.startswith("pr view 7 -R o/aaas-app-demo --json mergeCommit"):
            return json.dumps({"state": "MERGED", "mergeCommit": {"oid": SHA}})
        if a.startswith("pr list"):
            self.polls += 1
            assert "--head deploy/demo-bbbbbbb" in a
            return json.dumps([{"number": 31}] if self.polls >= self.pr_appears_after else [])
        if a.startswith("pr view 31"):
            return json.dumps({"headRefOid": "h" * 40, "headRefName": "deploy/demo-bbbbbbb", "baseRefName": "master"})
        if "check-runs" in a:
            return json.dumps({"check_runs": [{"id": 1, "name": "gate", "status": "completed", "conclusion": self.gate}]})
        if "/pulls/31/files" in a:
            return json.dumps([{"filename": f} for f in self.files])
        if "/pulls/31/merge" in a:
            self.merged.append(a)
            return "{}"
        if "contents/" + TF in a:
            data = OLD if "ref=master" in a else self.after
            return json.dumps({"content": base64.b64encode(json.dumps(data).encode()).decode()})
        if "applied.json" in a:
            self.applied_polls += 1
            image = NEW["container_image"] if self.applied_polls >= self.applied_after else OLD["container_image"]
            return json.dumps({"content": base64.b64encode(json.dumps({"image": image}).encode()).decode()})
        if "/issues/31/comments" in a:
            return json.dumps([{"body": self.comment}] if self.comment and self.applied_polls >= 1 else [])
        raise AssertionError(f"unexpected gh call: {a}")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


APP_PR = ck.PullRequest("o", "aaas-app-demo", 7)


def run(gh, **kw):
    c = Clock()
    steps = []
    d = dl.deliver("o", APP_PR, "deployments/dev/demo", progress=steps.append, run=gh, sleep=c.sleep, clock=c, **kw)
    return d, steps


def test_happy_path_merges_and_waits_for_the_record():
    gh = FakeGitHub(pr_appears_after=3, comment="### Deployed — x\n\n**URL:** https://app.io")
    d, steps = run(gh)
    assert d.live and d.app_url == "https://app.io" and d.problem == ""
    assert len(gh.merged) == 1 and "sha=" + "h" * 40 in gh.merged[0]
    assert steps == ["Packaging the new version", "Going live"]


def test_a_red_gate_is_not_merged():
    gh = FakeGitHub(gate="failure")
    d, _ = run(gh)
    assert not d.live and "plan failed" in d.problem and gh.merged == []


def test_more_than_an_image_is_not_merged():
    gh = FakeGitHub(files=(TF, "deployments/dev/demo/main.tf"))
    d, _ = run(gh)
    assert not d.live and "not merged by the harness" in d.problem and gh.merged == []


def test_a_failed_deploy_reports_its_cause():
    gh = FakeGitHub(applied_after=10**9,
                    comment="### Deploy failed — x\n\n**Reason:** not running\n**Cause:** `[migrate] FAILED: 23505`\n")
    d, _ = run(gh)
    assert not d.live and "23505" in d.problem and len(gh.merged) == 1


def test_no_deployment_pr_times_out_legibly():
    gh = FakeGitHub(pr_appears_after=10**9)
    d, _ = run(gh, find_timeout=60)
    assert not d.live and "no deployment PR" in d.problem


def test_live_without_a_record_is_still_live():
    gh = FakeGitHub(applied_after=10**9, comment="### Deployed — x\n\n**URL:** https://app.io")
    d, _ = run(gh, record_grace=60)
    assert d.live and "record" in d.problem
