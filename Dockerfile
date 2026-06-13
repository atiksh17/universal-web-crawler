# python:3.12-slim is Debian bookworm. Pin 3.12 — nodriver breaks on 3.14.
FROM python:3.12-slim

# Chromium + libs for nodriver (L2). Same browser the VPS runs — full parity.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium fonts-liberation libnss3 libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libgtk-3-0 libxss1 libasound2 libgbm1 libxshmfence1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# nodriver finds Chrome here (config default; override via .env if needed).
ENV CRAWLER_CHROME_PATH=/usr/bin/chromium
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY smoke.py benchmark.py ./

EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
