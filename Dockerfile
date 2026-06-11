# DeepSeek Infra — local-first agentic AI runtime
# 构建:  docker build -t deepseek-infra:2.1.7 .
# 运行:  docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -v deepseek-data:/data deepseek-infra:2.1.7
# 多架构: docker buildx build --platform linux/amd64,linux/arm64 -t deepseek-infra:2.1.7 .
# 说明见 docs/DEPLOYMENT.md

# --- builder：依赖装进独立 venv，编译垃圾与 pip 元数据留在本层 -----------------
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --- runtime：只带 venv + 源码 + 静态资源 --------------------------------------
FROM python:3.12-slim

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app.py ./
COPY deepseek_infra ./deepseek_infra
COPY static ./static

# 全部可写运行时数据（auth token / 缓存 / 向量索引 / trace / 记忆 / 任务快照）
# 经 DEEPSEEK_MOBILE_ROOT 集中到 /data，一个卷即可持久化；静态资源固定在镜像内。
ENV PATH="/opt/venv/bin:$PATH" \
    DEEPSEEK_MOBILE_ROOT=/data \
    DEEPSEEK_MOBILE_STATIC_DIR=/app/static \
    HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data && chown -R appuser:appuser /data
VOLUME ["/data"]

USER appuser
EXPOSE 8000

# /healthz 是不鉴权的 liveness 探针（slim 镜像无 curl，用 stdlib）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4)"]

CMD ["python", "app.py"]
