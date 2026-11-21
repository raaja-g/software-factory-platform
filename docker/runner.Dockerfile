# Standard runner image. Versions (looked up 2026-11, M0 must confirm):
#   debian trixie-slim, node v24.22.3 (24 LTS), uv 0.13.9. Every base is pinned by digest.
# Multi-arch build:
#   docker buildx build --platform linux/amd64,linux/arm64 -f docker/runner.Dockerfile -t <registry>/runner:<ver> --push .
FROM node:24-trixie-slim@sha256:af02d57b57719b9657a45199a79264a9437940cd55b3c7b3fc99615654f124a0 AS node
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates python3 python3-venv build-essential ripgrep socat jq procps \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/ /usr/local/
COPY --from=uv /uv /uvx /usr/local/bin/
RUN useradd -u 10001 -d /home/factory factory \
 && mkdir -p /work /home/factory \
 && chown 10001:10001 /work /home/factory
ENV HOME=/home/factory \
    UV_CACHE_DIR=/home/factory/.cache/uv \
    npm_config_cache=/home/factory/.npm \
    XDG_CACHE_HOME=/home/factory/.cache
ENV OPENCODE_CACHE=/opt/factory-engines/opencode-cache
# Engine CLIs (M6.1). WORKDIR is not "/" because the Claude Code installer hangs there.
# Lockfiles pin every transitive dependency with integrity hashes. Optional deps stay on so
# the native binaries land; install scripts are off.
WORKDIR /opt/factory-engines
COPY docker/engines/package.json docker/engines/package-lock.json ./
# Install scripts are off, but opencode-ai and claude-code need theirs to link the native binary.
RUN npm ci --omit=dev --ignore-scripts \
 && npm rebuild opencode-ai @anthropic-ai/claude-code \
 && mkdir -p ${OPENCODE_CACHE} /etc/factory \
 && ln -s /opt/factory-engines/node_modules/.bin/opencode /usr/local/bin/opencode \
 && ln -s /opt/factory-engines/node_modules/.bin/claude /usr/local/bin/claude \
 && ln -s /opt/factory-engines/node_modules/.bin/gemini /usr/local/bin/gemini \
 && npm ls --json --depth=0 | jq '{opencode: .dependencies["opencode-ai"].version, claude_code: .dependencies["@anthropic-ai/claude-code"].version, gemini_cli: .dependencies["@google/gemini-cli"].version}' > /etc/factory/engine-versions.json
# OpenCode provider packages, laid out as OpenCode's cache directory (read-only at run time;
# M6.4 copies it into the writable home).
COPY docker/engines/opencode-providers/package.json docker/engines/opencode-providers/package-lock.json ${OPENCODE_CACHE}/
RUN npm ci --omit=dev --ignore-scripts --prefix ${OPENCODE_CACHE} \
 && chown -R 10001:10001 ${OPENCODE_CACHE} /home/factory  # `npm rebuild` above ran as root with HOME=/home/factory
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /work
USER 10001
LABEL factory.engine.opencode="1.19.5" \
      factory.engine.claude_code="2.1.322" \
      factory.engine.gemini_cli="0.68.0"
