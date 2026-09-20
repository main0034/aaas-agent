# aaas-agent — the harness

Runs the AaaS agent for one task, in a container, against a fresh clone, with a
tool policy and a run record. Phase 4a of `POC-PLAN.md`.

The agent's *instructions* are not in this repository. `PROMPT.md` and the
runbooks live in `aaas-deployments/agent/`, under CODEOWNERS, next to the schema
they describe. The harness reads them out of the clone it just made. This
repository is the thing that constrains the agent, and the agent has no checkout
of it and no write access to it — which is the one reason it is a separate repo
rather than another directory in `aaas-deployments`.

```
run.sh                    host wrapper: builds the image, passes exactly two secrets
Dockerfile                the container: no az, no terraform, no cloud credentials
scripts/verify-isolation  asserts that claim on every start, and refuses to run if it fails
harness/main.py           session loop, streaming output, run record
harness/policy.py         tool allow/deny, as a PreToolUse hook
harness/workspace.py      clones the repos the agent works in
harness/report.py         timings, cost, tool histogram, refusals, PR URLs
tests/test_policy.py      the policy is the only part that decides anything
briefs/                   example requests
runs/                     transcripts and reports (gitignored)
```

```bash
pip install -r requirements-dev.txt && pytest -q     # 38 tests, no container needed
```

## Running it

```bash
claude setup-token                      # once; one-year token, no API billing
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
export GH_TOKEN=github_pat_...          # see below

./run.sh --request @briefs/room-booking.md
```

`ANTHROPIC_API_KEY` works too and takes precedence; either is accepted.

**The subscription token has a licence boundary worth knowing about.** Anthropic
restricts subscription OAuth to *individual* use — it covers one person proving
this on their own machine, and it does not cover AaaS running agents on behalf
of customers. The product needs API billing from the first customer onward, so
**per-customer agent inference is a COGS line the cost model does not currently
have.** Worth pricing before the tiers in OQ-1a are set, because it scales with
usage in a way the infrastructure floor does not.

Interactive by default: when the agent finishes a turn you get a prompt, so it
can ask for `owner` and `costCenter` the way the runbook tells it to. A blank
line ends the session. `--non-interactive` gives it exactly one turn, which is
the mode to use once you are timing runs rather than debugging them.

Output lands in `runs/<timestamp>/`: `report.md`, `report.json`,
`transcript.jsonl`, and the `workspace/` the agent worked in — left in place on
purpose, because the fastest way to understand a confusing run is to look at the
tree it produced.

## The token

A **fine-grained PAT scoped to `aaas-deployments` only**, with Contents and Pull
requests set to read and write. Not your `gh auth` token — that carries your
whole account, and the entire point here is knowing exactly what the agent can
reach.

The production answer is an installation token minted from the `aaas-bot` App
outside the container and injected with its one-hour life. Same shape, shorter
fuse; not worth building before the loop is proven.

## What the boundary actually is

The tool policy in `policy.py` is a **guidance layer**, not a security boundary,
and the file says so at the top. The agent has `python3` because it is required
to run `scripts/validate_deployment.py`, and anything that can run Python can
run `pip install azure-cli`. `python3` is therefore restricted to running the
validator — which closes the widest hole cheaply — but a command allowlist is
never the thing to trust.

What actually contains the agent, in order:

1. **There is no Azure credential in the container.** No service principal
   secret, no `~/.azure`, no `ARM_*` or `AZURE_*`. `run.sh` passes an explicit
   allowlist of two variables. This is the property the whole pipeline rests on
   (`FINDINGS.md` #11), and it holds even if every rule in `policy.py` is
   bypassed.
2. **No `az` and no `terraform` binary exist in the image**, and `curl` is
   purged after the build so ad-hoc fetching is genuinely unavailable.
3. `verify-isolation` asserts 1 and 2 on every start and refuses to run
   otherwise. Unverified assumptions about identity have already cost this
   project a day; a check that runs every time is cheap.

The policy layer's real value is different and still worth having: a refusal
that names the remedy is fed straight back to the model, so it corrects instead
of thrashing, and every refusal is recorded. **A refusal that recurs across runs
is a runbook problem, not an agent problem** — that is the feedback loop this
harness exists to produce.

## What gets measured

`report.md` per run: wall clock, per-turn timings, turn count, cost, a tool-call
histogram, every policy refusal, and any PR URL that appeared. The histogram is
what makes a slow run diagnosable — "twenty minutes" is not a finding, "forty
`git status` calls" is.

## Before you trust the first run

Two things in here are asserted rather than proven, and both are cheap to check
on the first run. Finding 2 in `FINDINGS.md` cost a day to an assumption about
identity that was never tested, so test these:

1. **The isolation check actually blocks.** `verify-isolation` runs on every
   start and prints `[ok]` — read that line rather than assuming it appeared.
   To see it fail, run with `-e ARM_CLIENT_ID=x` added and confirm the container
   refuses to start.
2. **A policy refusal actually fires.** Ask the agent, in the first run, to run
   `terraform plan` and check it comes back with the refusal text rather than a
   "command not found". The hook is the gate; a hook that silently does not run
   would look identical to a well-behaved agent until the day it isn't.

The image build itself is the one step never executed here — if it fails, the
GitHub CLI apt repository is the likely place.

## Known gaps

- **The container has general network access.** It needs `api.anthropic.com` and
  GitHub. Blocking Azure management endpoints would need an egress proxy, and it
  would buy nothing today because there is no credential to use against them.
  Worth revisiting if the agent ever runs unattended.
- **`--non-interactive` cannot ask for `owner` or `costCenter`**, so those must
  be in the brief. The runbook forbids guessing them; the agent should stop
  rather than invent, and a run that stops for that reason is behaving correctly.
- **Repo provisioning is not automated.** Creating an app repo needs the GitHub
  App installed on it and two secrets set, neither of which a scoped token can
  do. See `agent/create-app.md`; this is `FINDINGS.md` #3 restated as a task.
