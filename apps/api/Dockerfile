# ROS API - FastAPI on the MIT LangChain/LangGraph stack.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # The default local (fastembed) embedder's model is baked into the image at this path
    # (see the pre-download step below); the app reads the same dir at runtime, so ingestion
    # works fully offline with no first-run download. Outside the /app/.data volume, so it's
    # part of the image, not the persisted volume.
    ROS_FASTEMBED_CACHE_DIR=/app/.fastembed-cache

# Build deps for psycopg/cryptography wheels (kept minimal; most ship manylinux wheels).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build context is the REPO ROOT (see docker-compose.yml: context: . / dockerfile:
# apps/api/Dockerfile) so we can COPY packages/schemas, which lives above apps/api.
#
# Third-party deps install in a layer keyed only on pyproject.toml + a package stub, so a
# pure ros/ source edit does NOT invalidate this (expensive) layer. The editable install
# just links /app/ros onto sys.path, so overwriting the stub with the real tree afterwards
# needs no reinstall. (No BuildKit --mount=type=cache here: Railway's Metal builder rejects
# cache mounts lacking its proprietary cacheKey id prefix, and a Railway-specific id would
# break plain `docker build`/compose. Docker layer caching still skips this layer unchanged.)
COPY apps/api/pyproject.toml apps/api/README.md ./
RUN mkdir -p ros && touch ros/__init__.py
# Production extras: model providers, vectors, knowledge, MCP, workers (Redis/arq), Postgres.
RUN pip install --upgrade pip && \
    pip install -e ".[providers,vectors,knowledge,mcp,workers,postgres]"

# Real source over the stub (this layer changes on every edit, but it is AFTER the install).
COPY apps/api/ros ./ros
COPY apps/api/alembic.ini ./alembic.ini
COPY apps/api/migrations ./migrations
# Shared JSON Schemas live at the repo root, outside the old ./apps/api context. Bake them at
# /app/packages/schemas so config.py's schemas_dir default (/app/packages/schemas) resolves.
COPY packages/schemas ./packages/schemas

# Pre-download the default local embedder (fastembed) model into the image so the container
# ships self-contained: no ~130MB HuggingFace fetch on the first ingest, and no runtime egress
# to huggingface.co (which a locked-down deploy may block). Keep this model id in sync with
# ros.knowledge.embeddings._DEFAULT_FASTEMBED. Runs before the chown so the ros user owns it.
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir=os.environ['ROS_FASTEMBED_CACHE_DIR'])"

# Run as a non-root user. Pre-create /app/.data (holds the Fernet master.key + Chroma store)
# owned by ros: an empty named volume mounted there copies the image mountpoint's ownership on
# first use, so this keeps the volume ros-writable. Without it the volume would mount root-owned
# and the non-root process could not write the master key (PermissionError at startup).
RUN useradd --create-home --uid 10001 ros \
    && mkdir -p /app/.data \
    && chown -R ros:ros /app
USER ros

EXPOSE 8000

# Container healthcheck hits the readiness probe (DB + checkpointer).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/readyz || exit 1

# Apply migrations, then serve. (Schema also self-bootstraps via create_all, but on managed
# Postgres `alembic upgrade head` is the controlled path.)
# No uvicorn --proxy-headers/--forwarded-allow-ips: the app derives the client IP itself and
# trusts X-Forwarded-For only from ROS_TRUSTED_PROXIES (default none -> the real socket peer).
# Letting uvicorn rewrite the peer from XFF for ANY client (as --forwarded-allow-ips='*' did)
# lets clients spoof their IP to evade per-IP rate limits / poison audit logs. Behind a real
# proxy, set ROS_TRUSTED_PROXIES to the proxy IP(s).
CMD ["sh", "-c", "alembic upgrade head && uvicorn ros.main:app --host 0.0.0.0 --port 8000"]
