# syntax=docker/dockerfile:1
# The agent's container.
#
# The point of this image is what is NOT in it. There is no `az`, no
# `terraform`, no Azure SDK, no ~/.azure, and no credential that could reach a
# subscription. `verify-isolation` asserts that at start-up rather than trusting
# it, because FINDINGS.md #2 is the lesson that an unverified assumption about
# identity costs a day.
#
# `curl` is installed to fetch the GitHub CLI and then purged, so the policy
# layer's refusal of ad-hoc network calls is backed by the binary genuinely not
# being there.
#
# Runs as the image's existing non-root `node` user (uid 1000).
#
# This was root at first, on the reasoning that there is nothing in here worth
# escalating to and a non-root user risks uid-mismatch pain on macOS bind
# mounts. That reasoning was wrong in a way that had nothing to do with
# security: the Claude Code CLI refuses to run with skipped permissions as
# root, so the harness could not start at all. The papercut being avoided was
# hypothetical and the blocker was real.

# uv, only as a source to bind-mount from in the dependency step below. A
# --mount `from=` cannot take a build arg, hence a named stage.
ARG UV_VERSION=0.12.13
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM node:22-bookworm-slim

# The .NET SDK the application template pins in global.json. Keep the two in
# step: global.json says rollForward latestPatch, so an SDK from another feature
# band (10.0.1xx, 10.0.3xx) is refused by `dotnet` with an error about
# global.json rather than about this file.
ARG DOTNET_SDK_VERSION=10.0.401

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    DOTNET_ROOT=/usr/share/dotnet \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    NUGET_XMLDOC_MODE=skip

# The SDK is installed with Microsoft's install script, while curl is still
# here. It is the widest capability in the image: `dotnet build` and
# `dotnet test` execute whatever code the agent writes, with network egress for
# NuGet and GH_TOKEN in the environment. That is by design - the runbook's
# evidence of correctness is passing tests - and it is why the policy layer is
# not the boundary. See policy.py.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq python3 less libicu72 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && bash /tmp/dotnet-install.sh --version "$DOTNET_SDK_VERSION" --install-dir "$DOTNET_ROOT" --no-path \
    && ln -s "$DOTNET_ROOT/dotnet" /usr/local/bin/dotnet \
    && rm /tmp/dotnet-install.sh \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# The SDK drives the Claude Code CLI, so the CLI has to be here.
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# Python dependencies, from uv.lock, into a venv on Debian's own python3.
#
# uv is bind-mounted for this one step and never lands in the image, and the
# venv is created without pip, and python3-pip is not installed. So nothing in
# the running container can install a package - `pip install azure-cli` was the
# widest hole the README described, and verify-isolation now asserts it is shut.
# The lock pins the whole tree with hashes; requirements.txt pinned only the
# two top-level packages and let everything underneath them float.
#
# UV_PYTHON_DOWNLOADS=never: use bookworm's 3.11 (pyproject says ==3.11.*), and
# fail the build rather than quietly fetch a different interpreter.
COPY pyproject.toml uv.lock /tmp/aaas-agent/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    uv sync --locked --no-dev --project /tmp/aaas-agent \
    && rm -rf /tmp/aaas-agent
# First on PATH, so `python3` - the harness, and the agent running the
# deployment validator - is the venv's interpreter with jsonschema importable.
ENV PATH=/opt/venv/bin:$PATH

# Explicit octal, not `chmod +x`.
#
# COPY preserves the source file's mode, and files written by some editors and
# sync tools are owner-only (0600/0700). `chmod +x` then adds execute WITHOUT
# adding read, giving 0711 - which works for a compiled binary and fails for a
# script, because the kernel must read the file to hand it to the interpreter.
# As root that is invisible. As USER node it is "Permission denied" with no
# hint about file modes.
COPY scripts/verify-isolation /usr/local/bin/verify-isolation
RUN chmod 0755 /usr/local/bin/verify-isolation

# Two disposable-container conveniences, set before dropping privileges:
# --system so they apply to the node user, and safe.directory because a clone
# on a bind mount can otherwise trip git's dubious-ownership check with an
# error that says nothing about bind mounts.
RUN git config --system --add safe.directory '*' \
    && git config --system init.defaultBranch master

WORKDIR /work
COPY harness /work/harness
# Same reasoning: normalise modes rather than inheriting whatever the host had.
RUN mkdir -p /work/runs \
    && chown -R node:node /work \
    && chmod -R a+rX /work

USER node

ENTRYPOINT ["/usr/local/bin/verify-isolation"]
CMD ["python3", "-m", "harness.main", "--help"]
