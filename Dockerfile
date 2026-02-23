FROM python:3.12-slim

LABEL maintainer="ACNet-AI"
LABEL description="ACN - Agent Collaboration Network"

WORKDIR /app

# Install system dependencies including curl for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY acn/ ./acn/
COPY skills/ ./skills/

# Install Python dependencies
RUN pip install --no-cache-dir .

# Run as non-root user for security
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose port (Railway injects $PORT; default to 8000 for local/Docker Compose)
EXPOSE 8000

# Health check using curl (more reliable than python httpx import)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# Run application — use $PORT if set (Railway), otherwise default to 8000
CMD ["sh", "-c", "uvicorn acn.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
