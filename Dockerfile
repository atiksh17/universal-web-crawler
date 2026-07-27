# python:3.12-slim is Debian bookworm. Pin 3.12 — nodriver breaks on 3.14.
FROM python:3.12-slim

# Chromium + libs for nodriver (L2). Same browser the VPS runs — full parity.
# `curl` is not for the crawler itself — it is what container healthchecks shell
# out to. Without it an orchestrator (Coolify, compose, k8s) marks a perfectly
# healthy app unhealthy and rolls the deploy back.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium fonts-liberation libnss3 libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libgtk-3-0 libxss1 libasound2 libgbm1 libxshmfence1 \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# nodriver finds Chrome here (config default; override via .env if needed).
ENV CRAWLER_CHROME_PATH=/usr/bin/chromium
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY smoke.py benchmark.py ./
COPY scripts ./scripts

EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
