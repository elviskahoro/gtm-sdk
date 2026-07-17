# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.26@sha256:0c5ad5d5fefaa496b053f5d8dbdff7b04824808948c9fb18fb7bfeadc8c8aa89 AS uv

FROM python:3.13-slim-bookworm@sha256:56249d7a2f93306106f6d8bcdf6423afb73c1b747d874febcc778beee25cb8bb AS runtime-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && groupadd --gid 1000 runner \
    && useradd --uid 1000 --gid runner --create-home runner \
    && rm -rf /var/lib/apt/lists/*

FROM runtime-base AS dependency-builder

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /build
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-extras --dev --locked --no-install-project --compile-bytecode \
    --python /usr/local/bin/python

FROM runtime-base AS final

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --chown=runner:runner --from=dependency-builder /opt/venv /opt/venv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/runner
USER runner
HEALTHCHECK NONE
