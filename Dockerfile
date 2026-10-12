FROM python:3.12-slim

LABEL org.opencontainers.image.title="Infomaniak Sync"
LABEL org.opencontainers.image.description="Unraid-Shares mit Infomaniak kDrive synchronisieren"
LABEL org.opencontainers.image.source="https://github.com/PaulG67/infomaniak-sync"
LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/PaulG67/infomaniak-sync/main/icon.png"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    SHARES_ROOT=/shares \
    PORT=8794 \
    PUID=99 \
    PGID=100 \
    TZ=Europe/Zurich

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      unzip \
      tzdata \
      gosu \
      passwd \
    && curl -fsSL https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh \
    && mkdir -p /config /shares

VOLUME ["/config", "/shares"]

EXPOSE 8794

HEALTHCHECK --interval=60s --timeout=8s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8794'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=5).status==200 else 1)"

ENTRYPOINT ["/docker-entrypoint.sh"]
