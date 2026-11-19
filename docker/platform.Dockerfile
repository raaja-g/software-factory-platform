# Platform image (api and orchestrator). Bases looked up 2026-11 (uv 0.13.9, python 3.12-slim-trixie); M0 must confirm.
# Multi-arch build: docker buildx build --platform linux/amd64,linux/arm64 -f docker/platform.Dockerfile .
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY defaults ./defaults
COPY schema ./schema
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && apt-get clean \
 && groupadd -g 10001 factory \
 && useradd -u 10001 -g 10001 -d /home/factory -m factory \
 && mkdir -p /var/lib/factory/artifacts /gateway-config /pkg-auth \
 && chown 10001:10001 /var/lib/factory/artifacts /gateway-config /pkg-auth \
 && git config --system safe.directory /definition
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH
USER 10001
EXPOSE 8080
ENTRYPOINT ["factory"]
