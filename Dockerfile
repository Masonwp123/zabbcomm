FROM python:3.15-alpine

LABEL maintainer="Mason Parker <masonparker868@gmail.com>"
LABEL description="Receive device heartbeats and create zabbix items"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_COLORS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Add required packages, util-linux is for permissions
RUN apk update && apk upgrade --no-cache && \
    apk add --no-cache \
    util-linux

# uv installs exactly the versions in uv.lock, so every build of a commit gets the same dependencies
# Same version as CI and the uv_build requirement in pyproject.toml.
COPY --from=ghcr.io/astral-sh/uv:0.13.10 /uv /usr/local/bin/uv

# Dependencies first: this layer is reused as long as uv.lock does not change
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

# Then the application itself
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --no-cache

# Copy entrypoint and make executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Ensure entrypoint's line endings are Unix
RUN sed -i 's/\r$//g' /entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=3)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]