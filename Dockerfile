# =============================================================================
# Roostoo Trading Bot — Dockerfile
# =============================================================================
# Multi-stage build for minimal production image.
# =============================================================================

FROM python:3.11-slim AS base

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories
RUN mkdir -p /app/logs /app/state

# Non-root user
RUN useradd -r -s /bin/false botuser && chown -R botuser:botuser /app
USER botuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -sf http://localhost:8080/health || exit 1

# Expose dashboard port
EXPOSE 8080

# Entry point
CMD ["python", "-u", "main.py"]
