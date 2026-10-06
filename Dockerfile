FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8765 \
    LIST_PATH=/app/lists/router.txt \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 router-configurator \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app router-configurator \
    && mkdir -p /data /app/lists \
    && chown 10001:10001 /data

COPY router_configurator/ ./router_configurator/
COPY generate_geo_domains.py preset.example.txt ./

USER 10001:10001
EXPOSE 8765
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('PORT', '8765'))), timeout=2).close()"]

CMD ["python", "-m", "router_configurator", "--env-file", "/app/.env"]
