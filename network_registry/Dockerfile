FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY network_registry ./network_registry

RUN uv sync --frozen --no-dev

RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
