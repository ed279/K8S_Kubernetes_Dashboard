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

COPY k8s_dashboard_multiring.py ./

ENV PYTHONUNBUFFERED=1 \
    TZ=America/Los_Angeles \
    FLASK_ENV=production

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:5000/health || exit 1

CMD ["python3", "k8s_dashboard_multiring.py"]
