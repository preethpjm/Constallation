# Constellation: one image, any machine. Node, git and a C toolchain are included because onboarded tools may need them.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 CONSTELLATION_HOST=0.0.0.0 CONSTELLATION_PORT=8800 NODEFORGE_SETTINGS=/app/data/settings.json
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm build-essential curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# state lives in these folders; mount them as volumes so nodes, runs and settings survive upgrades
VOLUME ["/app/registry", "/app/runs", "/app/wrapped", "/app/crawled", "/app/data"]
RUN useradd -m app && chown -R app /app
USER app
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD curl -fs http://localhost:8800/api/health || exit 1
CMD ["python", "cli.py", "serve"]
