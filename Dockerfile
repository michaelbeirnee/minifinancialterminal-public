# Mini Financial Terminal — single-container image (API + web UI + CLI).
# Build:  docker build -t 5milliondollars .
# Run:    docker run -p 8000:8000 -v 5milliondollars-data:/data 5milliondollars
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY cli/ cli/
COPY docker-entrypoint.sh /usr/local/bin/

# Keep mutable state (SQLite DB + market-data cache) on /data so it lives in a
# volume rather than the container layer.
ENV MFT_DATABASE_URL=sqlite:////data/terminal.db \
    MFT_CACHE_DIR=/data/cache \
    MFT_DEBUG=false

# The entrypoint drops to this user once it has claimed the mounted volume, so
# the image starts as root and the running process still isn't.
RUN useradd --create-home terminal \
    && mkdir -p /data/cache \
    && chown -R terminal:terminal /data /app \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
VOLUME /data

EXPOSE 8000

# python stands in for curl, which the slim image doesn't ship.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/system/health', timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
