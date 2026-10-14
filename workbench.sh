#!/usr/bin/env bash
#
# The workbench (roadmap step 5a), from an application checkout:
#
#   workbench.sh check [name]    is requests/<name>.md complete enough to ship? (no agent, no network)
#   workbench.sh build [name]    build requests/<name>.md on change/<date>-<name>, no PR
#   workbench.sh rebuild         the request on this branch changed: build it again
#   workbench.sh ship [--merge]  spec-testers, the change record, a PR, checks and fix rounds.
#                                The PR waits for you; --merge lets the harness merge it when
#                                D-26 allows (no migration, no dependency)
#
# The request itself you write with whatever help you like - Rider's AI assistant given
# requests/TEMPLATE.md and AGENT.md works well - and `check` tells you what is missing.
#
# Run it from the app repo (Rider's "Workbench" run configurations do). It does the
# git a person would otherwise do around a run - commit the request, push it with
# your own git, check out and pull the branch the harness pushed - and runs
# ./run.sh in between. The harness clones from GitHub, never this checkout, so
# anything not pushed does not exist for it.
#
# Credentials come from the environment, or from the files next to aaas-agent:
#   CLAUDE_CODE_OAUTH_TOKEN  <- .claude-oauth-token-dont-delete
#   GH_TOKEN                 <- .aaas-agent-PAT-dont-delete (agent: deployments + app only)
#
# Plain bash 3.2: macOS's /bin/bash runs it.
set -Eeuo pipefail
# Never stop without saying where: set -e alone exits silently.
trap 'code=$?; echo "workbench: stopped at line $LINENO (exit $code): $BASH_COMMAND" >&2' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS="${AAAS_SECRETS_DIR:-$(dirname "$HERE")}"
cmd="${1:-}"
[ $# -gt 0 ] && shift
name=""
merge=0
for a in "$@"; do
  case "$a" in
    --merge) merge=1 ;;
    -*) echo "workbench: unknown option $a" >&2; exit 2 ;;
    *) name="$a" ;;
  esac
done

die() { echo "workbench: $*" >&2; exit 2; }
say() { printf '\033[1m%s\033[0m\n' "$*"; }

case "$cmd" in
  check | build | rebuild | ship) ;;
  *) die "usage: workbench.sh check [name] | build [name] | rebuild | ship [--merge]   (from the app repository)" ;;
esac

app="$(git rev-parse --show-toplevel 2>/dev/null)" || die "run this from inside an application checkout"
cd "$app"
repo="$(basename "$app")"
[ -f requests/TEMPLATE.md ] || die "$repo has no requests/TEMPLATE.md; is this an application repository?"
current="$(git rev-parse --abbrev-ref HEAD)"
# origin/HEAD exists only in a clone that recorded it; a checkout without it is normal.
base="$( (git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true) | sed 's#^origin/##')"
base="${base:-master}"

# The change's name: given, or this branch's, or the one request you are writing.
if [ -z "$name" ]; then
  case "$current" in
    change/*) name="$(echo "${current#change/}" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}-//')" ;;
  esac
fi
if [ -z "$name" ] && { [ "$cmd" = build ] || [ "$cmd" = check ]; }; then
  candidates="$(git status --porcelain --untracked-files=all -- requests | sed -E 's/^.. //' | grep -E '\.md$' | grep -v TEMPLATE.md || true)"
  if [ "$(printf '%s' "$candidates" | grep -c . || true)" = 1 ]; then
    name="$(basename "$candidates" .md)"
  fi
fi
[ -n "$name" ] || die "which change? Pass its name (requests/<name>.md), or run this on its change/ branch"
request="requests/$name.md"
[ "$name" != TEMPLATE ] || die "TEMPLATE is the template, not a request"

# The same completeness check the chat and ship apply (harness/intake.py), run here
# with the Mac's own python3: it needs nothing but the standard library.
check_request() {
  [ -f "$request" ] || die "no $request in this checkout"
  PYTHONPATH="$HERE" python3 -m harness.intake "$request"
}

# Credentials, without printing them.
credentials() {
  if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    [ -f "$SECRETS/.claude-oauth-token-dont-delete" ] || die "no CLAUDE_CODE_OAUTH_TOKEN and no $SECRETS/.claude-oauth-token-dont-delete"
    CLAUDE_CODE_OAUTH_TOKEN="$(tr -d '\n' < "$SECRETS/.claude-oauth-token-dont-delete")"
    export CLAUDE_CODE_OAUTH_TOKEN
  fi
  if [ -z "${GH_TOKEN:-}" ]; then
    [ -f "$SECRETS/.aaas-agent-PAT-dont-delete" ] || die "no GH_TOKEN and no $SECRETS/.aaas-agent-PAT-dont-delete"
    GH_TOKEN="$(tr -d '\n' < "$SECRETS/.aaas-agent-PAT-dont-delete")"
    export GH_TOKEN
  fi
}

# Get the request to GitHub, on the change branch. Only the request: anything else
# uncommitted stays yours, and stops a rebuild that would pull over it.
publish_request() {
  [ -f "$request" ] || die "no $request in this checkout"
  case "$current" in
    change/*) ;;
    "$base")
      # A new request, written on the default branch: it gets its branch here, with
      # the date the harness would give it (UTC).
      branch="change/$(date -u +%Y-%m-%d)-$name"
      git fetch --quiet origin
      if git ls-remote --heads origin | grep -Eq "refs/heads/change/[0-9]{4}-[0-9]{2}-[0-9]{2}-$name\$"; then
        die "a branch for $name exists on origin: check it out (git checkout change/<date>-$name) and work there"
      fi
      git checkout --quiet -b "$branch"
      current="$branch"
      ;;
    *) die "you are on $current: build from $base (a new request) or from its change/ branch" ;;
  esac
  if [ -n "$(git status --porcelain -- "$request")" ]; then
    git add -- "$request"
    git commit --quiet -m "request($name): $(sed -n 's/^# //p' "$request" | head -1)" -- "$request"
    say "Committed $request"
  fi
  if [ -n "$(git status --porcelain --untracked-files=no -- . ":!$request")" ]; then
    die "you have uncommitted changes besides the request; the harness would build over them. Commit or stash them first"
  fi
  git push --quiet -u origin "$current"
}

run() {
  local started
  started="$(date +%s)"
  "$HERE/run.sh" --task "$@" --app-repo "$repo" --change "$name"
  local code=$?
  say "$1 took $(( $(date +%s) - started ))s"
  return "$code"
}

case "$cmd" in
  check)
    check_request || exit 1
    ;;
  build | rebuild)
    # A build can start from an unfinished request; ship cannot. Say so now, not at ship.
    check_request || say "Building anyway: ship will refuse until the request is complete."
    credentials
    publish_request
    set +e; run "$cmd"; code=$?; set -e
    [ "$code" -eq 0 ] || exit "$code"
    git pull --quiet --ff-only origin "$current"
    say "Built. $current is checked out here at $(git rev-parse --short HEAD). Preview it: scripts/preview.sh (Rider: 2 Preview)"
    ;;
  ship)
    case "$current" in change/*) ;; *) die "ship from the change's branch (git checkout change/<date>-$name)";; esac
    git fetch --quiet origin
    if [ -n "$(git status --porcelain --untracked-files=no)" ] || [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$current")" ]; then
      die "this checkout differs from origin/$current. Ship ships what is on GitHub: commit, push or rebuild first"
    fi
    check_request || die "ship needs a complete request: fix it, rebuild, preview, then ship"
    credentials
    extra=()
    [ "$merge" = 1 ] && extra=(--auto-merge)
    set +e; run ship --spec-testers 2 --fix-rounds 2 ${extra[@]+"${extra[@]}"}; code=$?; set -e
    [ "$code" -eq 0 ] || exit "$code"
    git pull --quiet --ff-only origin "$current" || true
    if [ "$merge" = 1 ]; then
      say "Shipped. If the harness merged it, release.yml opens the deployment PR; it is yours to merge."
    else
      say "Shipped: the PR above is green and waits for you. After you merge it: git checkout $base && git pull"
    fi
    ;;
esac
