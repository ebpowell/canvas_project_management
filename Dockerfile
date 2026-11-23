# Build/Download the Canvas-Scraper Go CLI binary
FROM golang:1.23-bookworm AS go-builder

WORKDIR /src/canvas-scraper
RUN mkdir -p /out \
    && git clone --depth 1 https://github.com/jdansak/Canvas-Scraper.git . \
    && CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /out/canvas-scraper .


# Build Python dependencies with uv
FROM python:3.12-slim-bookworm AS py-builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev


# Minimal non-root runtime image
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:/usr/local/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH="/opt/pw-browsers"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=go-builder /out/canvas-scraper /usr/local/bin/canvas-scraper
COPY --from=py-builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=py-builder --chown=appuser:appuser /app/src /app/src

RUN mkdir -p /opt/pw-browsers /app/config /app/secrets /app/data \
    && /app/.venv/bin/playwright install --with-deps chromium \
    && chown -R appuser:appuser /opt/pw-browsers /app \
    && rm -rf /var/lib/apt/lists/*

USER appuser

HEALTHCHECK --interval=5m --timeout=15s --start-period=30s --retries=3 \
    CMD python -c "import canvas_tracker; print('healthy')" || exit 1

ENTRYPOINT ["python", "-m", "canvas_tracker.main"]
CMD ["--config", "/app/config/students.yaml"]
