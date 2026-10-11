"""
Delivery: from a merged app change to the new version running.

After the harness merges a change (D-26), the app's `release.yml` builds the image
and opens a deployment PR in aaas-deployments that bumps `container_image`. This
module follows that PR through, so that the conversation can tell the user "it is
live" rather than "it is merged":

  1. find the deployment PR for the merge commit (branch `deploy/<name>-<sha7>`)
  2. wait for its `gate` check
  3. merge it only if it changes exactly `container_image` in that deployment's
     tfvars and nothing else (`image_only`, deterministic - D-17)
  4. wait for `applied.json` on master to name the new image: the record job writes
     it only after a green readiness gate (D-22, D-28), so it is the observed state.
     A "Deploy failed" comment on the PR ends the wait early, with its reason.

The harness merges a deployment PR only after it merged the change itself, so a
change a human must review (a migration, a dependency) never reaches this module.
Everything that decides is a pure function; GitHub is behind `run`.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import checks as ck

DEPLOYMENTS_REPO = "aaas-deployments"
GATE = ("gate",)


class DeliveryError(RuntimeError):
    pass


@dataclass
class Delivery:
    live: bool
    pr_url: str = ""
    app_url: str = ""
    problem: str = ""
    seconds: float = 0.0


def deployment_path(app_checkout: Path) -> str:
    """`.aaas/deployment`'s `deployment_path`, the same line release.yml reads."""
    text = (app_checkout / ".aaas" / "deployment").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("deployment_path="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise DeliveryError(".aaas/deployment does not define deployment_path")


def deploy_branch(path: str, sha: str) -> str:
    """The branch release.yml pushes: `deploy/<basename>-<sha7>`."""
    return f"deploy/{Path(path).name}-{sha[:7]}"


def image_only(changed_files: list[str], tfvars: str, before: dict, after: dict) -> list[str]:
    """Why this deployment PR is more than an image bump. Empty means it is only that."""
    problems = []
    if changed_files != [tfvars]:
        problems.append(f"changes {', '.join(changed_files) or 'nothing'}, not only {tfvars}")
    keys = sorted(set(before) | set(after))
    other = [k for k in keys if k != "container_image" and before.get(k) != after.get(k)]
    if other:
        problems.append(f"changes {', '.join(other)} as well as container_image")
    if before.get("container_image") == after.get("container_image"):
        problems.append("does not change container_image")
    return problems


def applied_image(record: dict) -> str:
    return str(record.get("image", ""))


_URL_RE = re.compile(r"\*\*URL:\*\*\s*(https://\S+)")
_REASON_RE = re.compile(r"\*\*(Reason|Cause):\*\*\s*(.+)")


def read_deploy_comment(body: str) -> tuple[str, str, str] | None:
    """apply.yml's PR comment -> (ok|failed, app url, reason). None if it is not one."""
    if body.startswith("### Deployed"):
        m = _URL_RE.search(body)
        return ("ok", m.group(1) if m else "", "")
    if body.startswith("### Deploy failed"):
        m = _URL_RE.search(body)
        reasons = [f"{k.lower()}: {v.strip()}" for k, v in _REASON_RE.findall(body)]
        return ("failed", m.group(1) if m else "", "; ".join(reasons) or "the readiness gate failed")
    return None


# -- GitHub ---------------------------------------------------------------------

def _json(run: ck.Runner, args: list[str]):  # noqa: ANN202
    return json.loads(run(args) or "null")


def merge_commit(pr: ck.PullRequest, run: ck.Runner = ck.gh) -> str:
    data = _json(run, ["pr", "view", str(pr.number), "-R", pr.slug, "--json", "mergeCommit,state"])
    if data.get("state") != "MERGED" or not data.get("mergeCommit"):
        raise DeliveryError(f"{pr.url} is not merged")
    return data["mergeCommit"]["oid"]


def find_deploy_pr(owner: str, branch: str, run: ck.Runner = ck.gh) -> ck.PullRequest | None:
    data = _json(run, ["pr", "list", "-R", f"{owner}/{DEPLOYMENTS_REPO}", "--head", branch,
                       "--state", "all", "--json", "number"])
    return ck.PullRequest(owner, DEPLOYMENTS_REPO, data[0]["number"]) if data else None


def file_at(owner: str, path: str, ref: str, run: ck.Runner = ck.gh) -> str:
    data = _json(run, ["api", f"repos/{owner}/{DEPLOYMENTS_REPO}/contents/{path}?ref={ref}"])
    return base64.b64decode(data["content"]).decode("utf-8")


def pr_files(pr: ck.PullRequest, run: ck.Runner = ck.gh) -> list[str]:
    data = _json(run, ["api", f"repos/{pr.slug}/pulls/{pr.number}/files?per_page=100"])
    return sorted(f["filename"] for f in data)


def pr_comments(pr: ck.PullRequest, run: ck.Runner = ck.gh) -> list[str]:
    data = _json(run, ["api", f"repos/{pr.slug}/issues/{pr.number}/comments?per_page=100"])
    return [c.get("body", "") for c in data]


def deliver(
    owner: str,
    app_pr: ck.PullRequest,
    deployment: str,
    *,
    progress: Callable[[str], None] = lambda _s: None,
    run: ck.Runner = ck.gh,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    find_timeout: float = 15 * 60,
    gate_timeout: float = 20 * 60,
    apply_timeout: float = 45 * 60,
    record_grace: float = 10 * 60,
    poll: float = 15,
) -> Delivery:
    started = clock()
    sha = merge_commit(app_pr, run)
    branch = deploy_branch(deployment, sha)
    tfvars = f"{deployment}/terraform.tfvars.json"

    progress("Packaging the new version")
    pr = None
    while pr is None:
        pr = find_deploy_pr(owner, branch, run)
        if pr is None:
            if clock() - started >= find_timeout:
                return Delivery(False, problem=f"no deployment PR on {branch} after {int(find_timeout)}s "
                                               "(did the app's release workflow fail?)", seconds=clock() - started)
            sleep(poll)

    head = ck.pr_head(pr, run)
    v, _ = ck.wait_for_checks(lambda: ck.check_runs(pr, head["headRefOid"], run), required=GATE,
                              timeout=gate_timeout, poll=poll, sleep=sleep, clock=clock)
    if v.state != ck.GREEN:
        what = "did not finish" if v.state == ck.PENDING else "failed"
        return Delivery(False, pr.url, problem=f"the deployment plan {what} ({pr.url})", seconds=clock() - started)

    before = json.loads(file_at(owner, tfvars, head["baseRefName"], run))
    after = json.loads(file_at(owner, tfvars, head["headRefOid"], run))
    problems = image_only(pr_files(pr, run), tfvars, before, after)
    if problems:
        return Delivery(False, pr.url, problem=f"not merged by the harness: the deployment PR {'; '.join(problems)}",
                        seconds=clock() - started)
    if not after["container_image"].endswith(sha):
        return Delivery(False, pr.url, problem=f"the deployment PR deploys {after['container_image']}, not {sha[:7]}",
                        seconds=clock() - started)

    progress("Going live")
    ck.merge(pr, head["headRefOid"], run)
    applied_path = f"{deployment}/applied.json"
    went_live = clock()
    ok_seen: float | None = None
    while True:
        try:
            record = json.loads(file_at(owner, applied_path, "master", run))
        except (ck.ChecksError, json.JSONDecodeError, KeyError):
            record = {}
        outcome = next((o for o in (read_deploy_comment(b) for b in pr_comments(pr, run)) if o), None)
        if applied_image(record).endswith(sha):
            url = outcome[1] if outcome else ""
            return Delivery(True, pr.url, app_url=url, seconds=clock() - started)
        if outcome and outcome[0] == "ok":
            # The gate passed; the record job commits applied.json a minute later.
            # If it never does, the app is still live - say so rather than wait out the timeout.
            ok_seen = ok_seen or clock()
            if clock() - ok_seen >= record_grace:
                return Delivery(True, pr.url, app_url=outcome[1], seconds=clock() - started,
                                problem=f"live, but {applied_path} was not updated (the record job may have failed)")
        if outcome and outcome[0] == "failed":
            return Delivery(False, pr.url, app_url=outcome[1], problem=outcome[2], seconds=clock() - started)
        if clock() - went_live >= apply_timeout:
            return Delivery(False, pr.url, problem=f"{applied_path} did not show {sha[:7]} within "
                                                   f"{int(apply_timeout)}s of the merge", seconds=clock() - started)
        sleep(poll)
