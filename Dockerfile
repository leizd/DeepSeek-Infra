# DeepSeek Infra — local-first agentic AI runtime
# 构建:  docker build -t deepseek-infra:2.5.8 .
# 运行:  docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -v deepseek-data:/data deepseek-infra:2.5.8
# 说明见 docs/DEPLOYMENT.md
FROM python:3.12-slim

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# 先装依赖层，源码变更不破坏缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY deepseek_infra ./deepseek_infra
COPY static ./static
RUN find /app -type d -name __pycache__ -prune -exec rm -rf {} +

# 全部可写运行时数据（auth token / 缓存 / 向量索引 / trace / 记忆 / 任务快照）
# 经 DEEPSEEK_INFRA_ROOT（优先）或 DEEPSEEK_MOBILE_ROOT（兼容）集中到 /data，
# 一个卷即可持久化；静态资源固定在镜像内。
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

# /healthz 是不鉴权的 liveness 探针（slim 镜像无 curl，用 stdlib）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4)"]

CMD ["python", "app.py"]
