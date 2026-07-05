# ─── PashxD Backend — Cloud Run image ────────────────────────────────────
# Python 3.11.9 to match the previous Render runtime (runtime.txt).
FROM python:3.11.9-slim AS base

# Prevent Python from writing .pyc files and buffering stdout/stderr so logs
# stream straight to Cloud Logging.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: build tools only during install, then discarded in the final layer.
# tree-sitter wheels used by graphify are prebuilt, so no compiler is needed at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY . .

# Run as a non-root user (Cloud Run best practice / least privilege).
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Cloud Run injects PORT (default 8080). uvicorn binds to it.
ENV PORT=8080
EXPOSE 8080

# uvicorn handles SIGTERM for graceful shutdown; the FastAPI lifespan closes Mongo.
# Single uvicorn process — Cloud Run scales by adding container instances, not threads.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-graceful-shutdown 25"]
