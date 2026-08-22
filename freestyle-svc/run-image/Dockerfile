# ros-claude-backend run image for the NORTHFLANK execution provider.
#
# Reproducible, cached equivalent of the Freestyle exec-time bake (freestyle-svc/src/freestyle.ts
# TOOLCHAIN_STEPS): a container that can immediately run `python -m ros.runtime drive` AND host the
# claude_agent tool/node. Build this with a Northflank Build service; deploy the resulting image as
# the run Service (NORTHFLANK_RUN_IMAGE) that northflank.ts execs the runtime into.
#
# Baked runtimes: Python 3, Node 20+, Java (Amazon Corretto 21.0.2, pinned), gcc/build-essential,
# git, ripgrep, psql, sqlite3 — matching the Freestyle image so runs behave identically.
#
# Build args (mirror the Freestyle bake's env):
#   ROS_INSTALL_REPO_URL  plain https clone url (e.g. https://github.com/hvoguetech/agent-studio-backend.git)
#   ROS_INSTALL_TOKEN     token for a private clone (spliced as x-access-token; not persisted in the image)
#   ROS_INSTALL_REF       branch/tag to install (default: repo default branch)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    JAVA_HOME=/opt/jdk \
    PATH=/opt/jdk/bin:/usr/local/bin:/usr/bin:/bin

# System deps (parallels TOOLCHAIN_STEPS step 1).
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git curl ca-certificates gnupg jq ripgrep unzip \
      postgresql-client sqlite3 python3-venv python3-pip nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Java — Amazon Corretto 21.0.2 (pinned), arch-aware, to /opt/jdk (parallels TOOLCHAIN_STEPS step 2).
RUN set -eux; CORRETTO_VER=21.0.2.13.1; \
    ARCH="$(uname -m)"; case "$ARCH" in x86_64) CJ=x64;; aarch64|arm64) CJ=aarch64;; *) CJ=x64;; esac; \
    mkdir -p /opt/jdk; \
    curl -fsSL --retry 3 --max-time 300 \
      "https://corretto.aws/downloads/resources/${CORRETTO_VER}/amazon-corretto-${CORRETTO_VER}-linux-${CJ}.tar.gz" \
      -o /tmp/corretto.tgz; \
    tar -xzf /tmp/corretto.tgz -C /opt/jdk --strip-components=1; rm -f /tmp/corretto.tgz; \
    ln -sf /opt/jdk/bin/java /usr/local/bin/java; ln -sf /opt/jdk/bin/javac /usr/local/bin/javac

# Clone the ros backend + install the package with prod extras (parallels TOOLCHAIN_STEPS steps 3-4).
# ROS_INSTALL_TOKEN is spliced in at build time only; it is NOT written into any image layer/env.
ARG ROS_INSTALL_REPO_URL
ARG ROS_INSTALL_TOKEN=
ARG ROS_INSTALL_REF=
RUN set -eux; \
    CLONE_URL="$ROS_INSTALL_REPO_URL"; \
    if [ -n "$ROS_INSTALL_TOKEN" ]; then \
      CLONE_URL="$(printf '%s' "$ROS_INSTALL_REPO_URL" | sed -E "s#^https://#https://x-access-token:${ROS_INSTALL_TOKEN}@#")"; \
    fi; \
    git clone --depth 1 ${ROS_INSTALL_REF:+--branch "$ROS_INSTALL_REF"} "$CLONE_URL" /opt/ros-src; \
    python3 -m pip install -q -e "/opt/ros-src/apps/api[providers,vectors,knowledge,mcp,workers,postgres,storage,claude_code]"

# Claude Code CLI on PATH — prefer the SDK's bundled linux binary (deterministic), else npm (mirrors
# the Freestyle bake's TOOLCHAIN_STEPS claude step). The ONLY change vs. Freestyle is guarding against
# linking the target onto itself: the prior version resolved CLAUDE_BIN via `command -v claude` to
# /usr/local/bin/claude and then `ln -sf` made it point at itself (broken self-symlink). We never resolve
# to $LINK and never link a path onto itself. Non-fatal if absent (do NOT gate the build — Freestyle
# doesn't; a hard `claude --version || exit 1` gate previously failed the whole image build).
RUN set -eux; \
    LINK=/usr/local/bin/claude; \
    CLAUDE_BIN="$(find / -type f -path '*claude_agent_sdk*/_bundled/claude' 2>/dev/null | head -n1)"; \
    if [ -z "$CLAUDE_BIN" ]; then \
      npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 || true; \
      CLAUDE_BIN="$(find / -type f -path '*claude-code-linux-*/claude' 2>/dev/null | head -n1)"; \
      if [ -z "$CLAUDE_BIN" ]; then \
        RESOLVED="$(command -v claude 2>/dev/null || true)"; \
        [ -n "$RESOLVED" ] && [ "$RESOLVED" != "$LINK" ] && CLAUDE_BIN="$(readlink -f "$RESOLVED")"; \
      fi; \
    fi; \
    if [ -n "$CLAUDE_BIN" ] && [ -f "$CLAUDE_BIN" ] && [ "$CLAUDE_BIN" != "$LINK" ]; then \
      chmod +x "$CLAUDE_BIN" || true; ln -sf "$CLAUDE_BIN" "$LINK"; \
    fi; \
    claude --version >/dev/null 2>&1 && echo "claude OK" || echo "CLAUDE CLI NOT FOUND (non-fatal)"

# Bake shared JSON schemas where config.py expects them (parallels TOOLCHAIN_STEPS step 6).
RUN mkdir -p /app && cp -r /opt/ros-src/packages /app/packages || true

# Stable workspace root for the claude_code node's per-node dir <base>/<workflow_id>/<node_id>
# (matches the runtime's ROS_CLAUDE_CODE_WORKSPACE=/workspace default).
RUN mkdir -p /workspace
ENV ROS_CLAUDE_CODE_WORKSPACE=/workspace \
    IS_SANDBOX=1

WORKDIR /opt/ros-src/apps/api
# The container idles; northflank.ts execs `python -m ros.runtime drive ...` into it per run.
CMD ["sh", "-c", "sleep infinity"]
