FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml ./
COPY cloudhelm ./cloudhelm
COPY cloudhelm_agent ./cloudhelm_agent
RUN python -m pip wheel --wheel-dir /wheels ".[agent]"

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=builder /wheels /wheels
RUN apt-get update \
    && apt-get install --yes --no-install-recommends tzdata util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir /wheels/* \
    && rm -rf /wheels \
    && mkdir -p /data
WORKDIR /app
VOLUME ["/data"]
COPY --chmod=0755 deploy/agent-entrypoint.sh /usr/local/bin/cloudhelm-agent-entrypoint
ENTRYPOINT ["cloudhelm-agent-entrypoint"]
CMD ["python", "-m", "cloudhelm_agent.main"]
