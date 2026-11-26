FROM python:3.15-alpine

LABEL maintainer="Mason Parker <masonparker868@gmail.com>"
LABEL description="Receive device heartbeats and create zabbix items"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_COLORS=0 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3

WORKDIR /app

# Install locked dependencies first so this layer is cached across source changes.
# uv is mounted only for the build step, so it doesn't end up in the final image.
COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.13.8,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-install-project

# README.md is named in pyproject.toml, so building the project needs it.
COPY README.md ./
COPY src ./src
RUN --mount=from=ghcr.io/astral-sh/uv:0.13.8,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-editable

# Registry snapshot location. A new named volume mounted here inherits this ownership,
# which lets the unprivileged user write to it.
RUN mkdir /app/data && chown nobody:nobody /app/data

ENV PATH="/app/.venv/bin:$PATH"

USER nobody

ENTRYPOINT ["zabbcomm"]
