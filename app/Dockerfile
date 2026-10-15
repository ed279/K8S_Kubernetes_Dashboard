FROM python:3.12-slim

# DejaVu fonts (Linux equivalent of macOS SF Mono) + libcap for docker.sock
# tzdata so Python's zoneinfo can render America/Los_Angeles correctly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        ca-certificates \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY render.py collect.py server.py k8s_alerts_enricher.py ./

ENV PYTHONUNBUFFERED=1 \
    RENDER_INTERVAL=10 \
    TZ=America/Los_Angeles \
    AI_ROUTER_LOG=/var/log/openclaw/ai_router.jsonl \
    HOST_STATE=/dev/shm/cephalobot/host_state.json \
    CLAUDE_QUOTA_PATH=/dev/shm/cephalobot/claude_quota.json \
    DOCKER_SOCK=/var/run/docker.sock \
    OLLAMA_PORTS=11434,11435,11436 \
    HTTP_PORT=8088

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8088/healthz || exit 1

CMD ["python3", "server.py"]
