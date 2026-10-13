# Signals, hosted: the engine (SIGNALS_HOSTED=1). Railway builds this.
# The store (the Worth following result, the price cache) lives on a volume mounted at /data.
# One process only: the Worth following check and the caches live in its memory.
FROM python:3.14-slim

WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.21
COPY pyproject.toml uv.lock ./
COPY signals ./signals
RUN uv sync --frozen --no-dev

ENV SIGNALS_HOSTED=1 \
    SIGNALS_STORE_DIR=/data \
    PYTHONUNBUFFERED=1
# No access log: it would keep visitors' IP addresses (and, beside them, the alert links they tap).
CMD ["sh", "-c", "exec .venv/bin/uvicorn signals.app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
