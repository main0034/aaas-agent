#!/usr/bin/env bash
#
# Host-side wrapper. Builds the image if needed and starts one agent run.
#
# The important line in this file is the `docker run` invocation: it passes an
# explicit allowlist of two environment variables. It does not use --env-file,
# it does not pass -e with a bare name that would inherit from the shell, and it
# mounts nothing except the runs directory. Your shell almost certainly has an
# az login and ARM_* variables in it from working on the module; none of that
# reaches the container, and verify-isolation checks that claim rather than
# taking it on trust.
#
# Usage:
#   ./run.sh --request "an internal tool for booking meeting rooms"
#   ./run.sh --request @briefs/room-booking.md --non-interactive
#   ./run.sh --task create-deployment --request "..." --model <model-id>

set -euo pipefail

IMAGE="${AAAS_AGENT_IMAGE:-aaas-agent:local}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNS_DIR="${AAAS_RUNS_DIR:-$HERE/runs}"

missing=0
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  cat >&2 <<'EOF'
No Anthropic credential.

If you have a Claude subscription and no API billing, generate a long-lived
token once - it lasts a year and does not consume API credit:

  claude setup-token
  export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...

Anthropic restricts subscription OAuth to INDIVIDUAL use. That covers proving
this POC on your own machine. It does not cover AaaS serving customers - the
agent plane needs API billing the moment there is a second person, which is a
cost line the product model does not currently have.

Otherwise: export ANTHROPIC_API_KEY=sk-ant-...
EOF
  missing=1
fi
if [ -z "${GH_TOKEN:-}" ]; then
  cat >&2 <<'EOF'
GH_TOKEN is not set.

Use a fine-grained personal access token scoped to aaas-deployments only, with
Contents: read and write, and Pull requests: read and write. Nothing else.

  export GH_TOKEN=github_pat_...

Not your `gh auth` token: that one carries your whole account, and the point of
this exercise is to know exactly what the agent can reach. The eventual answer
is an installation token minted from the aaas-bot App outside the container and
injected with its one-hour life, which is the same shape with a shorter fuse.
EOF
  missing=1
fi
[ "$missing" -eq 0 ] || exit 2

if ! docker image inspect "$IMAGE" >/dev/null 2>&1 || [ "${AAAS_REBUILD:-0}" = "1" ]; then
  echo "Building $IMAGE..."
  docker build -t "$IMAGE" "$HERE"
  echo ""
fi

mkdir -p "$RUNS_DIR"

# Two credentials in. runs/ writable for the record, briefs/ read-only so a
# request can be kept in a file and edited without rebuilding the image.
# Nothing else is mounted: a path that exists on the laptop does not exist in
# here unless it appears on this list.
exec docker run --rm -it \
  -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
  -e CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}" \
  -e GH_TOKEN="$GH_TOKEN" \
  -e AAAS_GITHUB_OWNER="${AAAS_GITHUB_OWNER:-main0034}" \
  -v "$RUNS_DIR:/work/runs" \
  -v "$HERE/briefs:/work/briefs:ro" \
  "$IMAGE" \
  python3 -m harness.main --runs-dir /work/runs "$@"
