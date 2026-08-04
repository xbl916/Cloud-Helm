FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml ./
COPY cloudhelm ./cloudhelm
COPY cloudhelm_agent ./cloudhelm_agent
RUN python -m pip wheel --wheel-dir /wheels ".[server]" "psycopg>=3.2,<4"

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system cloudhelm \
    && useradd --system --gid cloudhelm --home-dir /app cloudhelm \
    && mkdir -p /data /app && chown cloudhelm:cloudhelm /data /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER cloudhelm
WORKDIR /app
EXPOSE 8080
VOLUME ["/data"]
CMD ["uvicorn", "cloudhelm.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
