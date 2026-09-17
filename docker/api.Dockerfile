ARG DOCKER_HUB_PREFIX=
FROM ${DOCKER_HUB_PREFIX}python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 storyloom \
    && useradd --uid 10001 --gid storyloom --no-create-home --home-dir /app --shell /usr/sbin/nologin storyloom

COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.lock.txt

COPY --chown=storyloom:storyloom backend ./backend
COPY docker/worker-healthcheck.py /usr/local/bin/storyloom-worker-healthcheck
RUN chmod 0755 /usr/local/bin/storyloom-worker-healthcheck \
    && mkdir -p /app/data \
    && chown storyloom:storyloom /app/data

USER storyloom
EXPOSE 8000
STOPSIGNAL SIGTERM

# Only the compose bridge may set X-Forwarded-For. Trusting "*" let any client spoof its own
# address, which also defeated the per-IP request limits.
ENV STORYLOOM_TRUSTED_PROXY="${STORYLOOM_TRUSTED_PROXY:-172.28.0.0/16}"
CMD ["sh", "-c", "exec python -m uvicorn backend.production_app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${STORYLOOM_TRUSTED_PROXY}\""]
