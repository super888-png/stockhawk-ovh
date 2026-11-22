FROM python:3.12-slim
# FORWARDED_ALLOW_IPS: only trust X-Forwarded-For from these proxies (override in .env when behind Caddy/Nginx, e.g. 172.16.0.0/12)
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Asia/Shanghai FORWARDED_ALLOW_IPS=127.0.0.1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN useradd -u 1000 -M -s /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data \
    && chmod 755 /docker-entrypoint.sh
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
# Entrypoint runs as root only long enough to chown the bind-mounted /data, then drops to app.
# Must stay single-process (--workers 1): background sniper tasks run in-process.
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--timeout-graceful-shutdown", "10"]
