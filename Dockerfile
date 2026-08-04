FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=4399

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard.py \
     tactic.py \
     game_stats.py \
     tactic_config.py \
     status.py \
     docker-entrypoint.py \
     ./

RUN useradd --system --uid 10001 --home-dir /app arena \
    && mkdir -p /app/runtime \
    && chown -R arena:arena /app

USER arena

EXPOSE 4399

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4399/api/state', timeout=3)"

ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
