FROM python:3.11-slim

LABEL maintainer="AgentPlanet Team <team@agentplanet.com>"
LABEL description="ACN - Agent Collaboration Network"

WORKDIR /app

# Install dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Copy application
COPY acn/ ./acn/

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health')"

# Run application
CMD ["uvicorn", "acn.api:app", "--host", "0.0.0.0", "--port", "8000"]





