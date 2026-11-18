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
WORKDIR /work
USER 10001
