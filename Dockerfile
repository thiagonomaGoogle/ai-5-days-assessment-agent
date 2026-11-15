# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

WORKDIR /build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY c4_architect_agent ./c4_architect_agent
COPY evaluations ./evaluations

RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

# Run as non-root user for container security best practices
RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 --gid appgroup --create-home --shell /sbin/nologin appuser

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    C4_SESSION_DB_URL=sqlite+aiosqlite:////app/data/c4_sessions.db \
    C4_KNOWLEDGE_DB_PATH=/app/data/c4_knowledge_store.sqlite3

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

COPY c4_architect_agent ./c4_architect_agent
COPY evaluations ./evaluations
RUN mkdir -p /app/data && chown -R appuser:appgroup /app

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

# Serves the ADK FastAPI web/API server on Cloud Run's $PORT
CMD ["sh", "-c", "adk api_server --host 0.0.0.0 --port ${PORT:-8080} ."]
