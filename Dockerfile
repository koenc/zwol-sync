# ==============================================================================
# ZWOL Sync - Dockerfile
# Geoptimaliseerd voor Synology NAS (/volume1/docker/zwol-sync)
# ==============================================================================

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

EXPOSE 8505

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl --fail http://localhost:8505/health || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8505", "--workers", "2", "--threads", "4", "app.main:app"]

