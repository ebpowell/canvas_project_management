# Stage 1: Build/Download Go Canvas-Scraper binary
FROM golang:1.23-alpine AS go-builder
WORKDIR /go/src/scraper
RUN apk add --no-cache git ca-certificates
RUN git clone --depth 1 https://github.com/jdansak/Canvas-Scraper.git . 2>/dev/null || true
RUN if [ -f "go.mod" ]; then go build -ldflags="-s -w" -o /usr/local/bin/canvas-scraper; else echo '#!/bin/sh\nexit 1' > /usr/local/bin/canvas-scraper && chmod +x /usr/local/bin/canvas-scraper; fi

# Stage 2: Build Python dependencies with uv
FROM python:3.12-slim AS py-builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./
RUN if [ -f "uv.lock" ]; then \
        uv sync --frozen --no-dev --no-install-project; \
    else \
        uv sync --no-dev --no-install-project; \
    fi

COPY src/ ./src/
RUN uv pip install --no-deps -e .

# Stage 3: Minimal production runtime
FROM python:3.12-slim AS runtime

# Create non-root system user
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -d /app -s /sbin/nologin -c "Canvas Tracker User" appuser

WORKDIR /app

# Copy Go binary from go-builder
COPY --from=go-builder /usr/local/bin/canvas-scraper /usr/local/bin/canvas-scraper

# Copy virtualenv and application code from py-builder
COPY --from=py-builder /app/.venv /app/.venv
COPY --from=py-builder /app/src /app/src

# Set environment PATH to use virtualenv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Prepare data and config directories
RUN mkdir -p /app/data /app/config /app/secrets && \
    chown -R appuser:appuser /app

USER appuser

HEALTHCHECK --interval=5m --timeout=15s --start-period=30s --retries=3 \
    CMD python3 -c "import canvas_tracker; print('healthy')" || exit 1

ENTRYPOINT ["canvas-tracker"]
