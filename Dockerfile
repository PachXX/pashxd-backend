# ── PashxD API — Cloud Run production image ─────────────────────────
# Build:  docker build -t pashxd-api .
# Run:    docker run -p 8080:8080 -e MONGO_URL=... pashxd-api

# Override in restricted networks, e.g. mirror.gcr.io/library/python:3.11-slim
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps in a separate layer so app-code changes don't re-install
FROM base AS deps
WORKDIR /app
COPY requirements.txt .
# Optional CA bundle for TLS-intercepting proxies:
#   docker build --secret id=cacert,src=/path/to/ca-bundle.crt ...
# The secret is optional; without it pip talks to PyPI directly.
RUN --mount=type=secret,id=cacert,target=/tmp/build-ca.crt \
    sh -c 'if [ -s /tmp/build-ca.crt ]; then export PIP_CERT=/tmp/build-ca.crt; fi; \
    pip install --prefix=/install -r requirements.txt'

FROM base AS runtime
WORKDIR /app

# Run as non-root (Cloud Run best practice)
RUN groupadd -r app && useradd -r -g app app

COPY --from=deps /install /usr/local
COPY main.py ./
COPY app ./app

USER app

# Cloud Run injects PORT (default 8080). Single uvicorn process per
# instance; concurrency is handled by asyncio + Cloud Run autoscaling.
ENV PORT=8080
EXPOSE 8080

# Shell form so ${PORT} expands; uvicorn handles SIGTERM for graceful
# shutdown (runs the FastAPI lifespan teardown, closes Mongo client).
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --timeout-graceful-shutdown 10
