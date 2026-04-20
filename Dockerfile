# syntax=docker/dockerfile:1
# ──────────────────────────────────────────────────────────────────────
# Stage 1: builder — installs build toolchain, compiles & installs deps.
# Nothing from this stage ships in the final image.
# ──────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
RUN pip install --upgrade pip "setuptools>=80.0" "wheel>=0.46.2" && \
    pip install -r requirements.txt

COPY . .
RUN pip install --no-deps .

# ──────────────────────────────────────────────────────────────────────
# Stage 2: runtime — slim base, no compilers, no curl. Healthcheck uses
# python's stdlib so we drop curl from the runtime layer entirely.
# ──────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRANSPORT_MODE=http \
    HTTP_PORT=8080 \
    HTTP_HOST=0.0.0.0

WORKDIR /app

# Pull installed Python packages and console scripts from the builder.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Ensure build tools in the runtime layer match upgraded versions from builder.
# python:3.11-slim ships old pip/wheel/setuptools; COPY --from=builder merges
# rather than replaces, so stale dist-info dirs can remain alongside new ones.
RUN pip install --upgrade "wheel>=0.46.2" "setuptools>=80.0.0"

RUN mkdir -p logs chroma_db && \
    useradd -m -u 1000 mcpuser && \
    chown -R mcpuser:mcpuser /app

USER mcpuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import sys, urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz', timeout=5).status == 200 else 1)"

CMD ["maximo-enterprise-mcp", "--http"]
