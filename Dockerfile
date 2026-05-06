FROM python:3.11-slim

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        wget \
    && rm -rf /var/lib/apt/lists/*

COPY handingtime_web/requirements.txt /app/handingtime_web/requirements.txt
RUN pip install --no-cache-dir -r /app/handingtime_web/requirements.txt
RUN python -m playwright install chromium

COPY EPUS_2ht /app/EPUS_2ht
COPY handingtime_web /app/handingtime_web
COPY README.md /app/README.md

ENV HT_WEB_HOST=0.0.0.0 \
    HT_WEB_PORT=8765 \
    HT_WEB_BASE_PATH=/handingtime \
    HT_PLAYWRIGHT_BOOTSTRAP=0 \
    HT_PLAYWRIGHT_AUTO_INSTALL=0 \
    ECCANG_DATA_DIR=/app/handingtime_web/data/auth

EXPOSE 8765

CMD ["python", "-u", "handingtime_web/server.py"]
