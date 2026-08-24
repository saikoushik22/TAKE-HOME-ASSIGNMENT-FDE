# syntax=docker/dockerfile:1

# Slim rather than alpine: asyncpg and nh3 ship manylinux wheels, and alpine's
# musl would force a source build of both, adding minutes and a compiler
# toolchain to the image for no runtime benefit.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies are their own layer, so editing application code does not
# re-resolve and re-download the entire dependency tree on every build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Non-root. The container writes nothing outside /data, which is a bind mount
# owned by the host, so there is no reason to run privileged.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

ENV DATA_DIR=/data

EXPOSE 8000

# Explicit IPv4 loopback rather than "localhost": in-container localhost also
# resolves to ::1, and a server bound to 0.0.0.0 accepts IPv4 only. This one
# happens to work because Python tries IPv4 first, but relying on resolver
# ordering for a healthcheck is luck, not design.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

# One worker on purpose. Each in-flight turn holds a database session for the
# whole generation, and CPU-bound local inference is the real bottleneck long
# before HTTP concurrency is. Scale with replicas, not workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
