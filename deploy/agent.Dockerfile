FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml ./
COPY cloudhelm ./cloudhelm
COPY cloudhelm_agent ./cloudhelm_agent
RUN python -m pip wheel --wheel-dir /wheels ".[agent]"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels && mkdir -p /data
WORKDIR /app
VOLUME ["/data"]
CMD ["python", "-m", "cloudhelm_agent.main"]
