# DeepSeek Infra - local-first Personal AI Runtime
# Build: docker build -t deepseek-infra:4.3.3 .
# Run: docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -v deepseek-data:/data deepseek-infra:4.3.3
# See docs/DEPLOYMENT.md for deployment notes.
FROM node:24-bookworm-slim AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build
RUN test -f /build/static/ui/index.html

FROM python:3.12-slim

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="DeepSeek Infra" \
      org.opencontainers.image.version="4.3.3" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.description="Python-first hybrid Personal AI Runtime"

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Install Python dependencies first so Docker can reuse the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY deepseek_infra ./deepseek_infra
COPY static ./static
COPY --from=frontend-builder /build/static/ui ./static/ui
RUN test -f /app/static/ui/index.html
RUN find /app -type d -name __pycache__ -prune -exec rm -rf {} +

# Store writable runtime data under /data: auth tokens, caches, indexes,
# traces, memory, generated artifacts, media objects and task snapshots.
# DEEPSEEK_INFRA_ROOT is the canonical runtime root; the mobile alias stays
# pointed at the same location for Android/shared-runtime compatibility.
ENV DEEPSEEK_INFRA_ROOT=/data \
    DEEPSEEK_MOBILE_ROOT=/data \
    DEEPSEEK_INFRA_STATIC_DIR=/app/static \
    DEEPSEEK_MOBILE_STATIC_DIR=/app/static \
    HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data && chown -R appuser:appuser /data
VOLUME ["/data"]

USER appuser
EXPOSE 8000

# Health check uses Python stdlib so the slim image does not need curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4)"]

CMD ["python", "app.py"]
