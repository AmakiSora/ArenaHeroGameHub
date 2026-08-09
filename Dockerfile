FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=4399 \
    TZ=Asia/Shanghai

# tzdata gives the container a real timezone database so time.strftime /
# time.localtime render Beijing time, not UTC. Without it, the battle-log
# panel and entrypoint log stamps would be 8 hours behind local time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard.py \
     tactic.py \
     game_stats.py \
     state_io.py \
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
