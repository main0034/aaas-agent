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
# Runs as root on purpose. There is nothing in here worth escalating to - two
# scoped tokens and a scratch clone - and a non-root user buys a uid-mismatch
# class of bind-mount failure on macOS that would cost more time than it saves.

FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq python3 python3-pip less \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# The SDK drives the Claude Code CLI, so the CLI has to be here.
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY scripts/verify-isolation /usr/local/bin/verify-isolation
RUN chmod +x /usr/local/bin/verify-isolation

WORKDIR /work
COPY harness /work/harness

ENTRYPOINT ["/usr/local/bin/verify-isolation"]
CMD ["python3", "-m", "harness.main", "--help"]
