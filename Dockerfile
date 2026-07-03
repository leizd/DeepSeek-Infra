# DeepSeek Infra 鈥?local-first agentic AI runtime
# 鏋勫缓:  docker build -t deepseek-infra:2.7.1 .
# 杩愯:  docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -v deepseek-data:/data deepseek-infra:2.7.1
# 璇存槑瑙?docs/DEPLOYMENT.md
FROM python:3.12-slim

# 闈?root 杩愯
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# 鍏堣渚濊禆灞傦紝婧愮爜鍙樻洿涓嶇牬鍧忕紦瀛?
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY deepseek_infra ./deepseek_infra
COPY static ./static
RUN find /app -type d -name __pycache__ -prune -exec rm -rf {} +

# 鍏ㄩ儴鍙啓杩愯鏃舵暟鎹紙auth token / 缂撳瓨 / 鍚戦噺绱㈠紩 / trace / 璁板繂 / 浠诲姟蹇収锛?
# 缁?DEEPSEEK_INFRA_ROOT锛堜紭鍏堬級鎴?DEEPSEEK_MOBILE_ROOT锛堝吋瀹癸級闆嗕腑鍒?/data锛?
# 涓€涓嵎鍗冲彲鎸佷箙鍖栵紱闈欐€佽祫婧愬浐瀹氬湪闀滃儚鍐呫€?
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

# /healthz 鏄笉閴存潈鐨?liveness 鎺㈤拡锛坰lim 闀滃儚鏃?curl锛岀敤 stdlib锛?
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4)"]

CMD ["python", "app.py"]
