# syntax=docker/dockerfile:1
# Product images. One build, three small targets that share a base layer:
#   gateway         - incident gateway (webhook/queue ingestion, durable task worker, approvals)
#   mcp             - one MCP capability server per container (oncall-mcp <server>)
#   scripted-model  - deterministic OpenAI-compatible model for CI and keyless demos (not for production)
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml VERSION ./
COPY schemas ./schemas
COPY src ./src
RUN useradd --system --uid 10001 --home /app oncall
ENV ONCALL_SCHEMA_DIR=/app/schemas

FROM base AS gateway
RUN pip install ".[gateway]" && rm -rf /root/.cache
USER 10001
EXPOSE 7100
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7100/healthz')"
ENTRYPOINT ["oncall-gateway", "--host", "0.0.0.0", "--port", "7100"]

FROM base AS mcp
RUN pip install ".[mcp]" && rm -rf /root/.cache
USER 10001
ENV ONCALL_MCP_HOST=0.0.0.0
ENTRYPOINT ["oncall-mcp"]

FROM base AS scripted-model
RUN pip install ".[scripted-model]" && rm -rf /root/.cache
USER 10001
EXPOSE 7199
ENTRYPOINT ["oncall-scripted-model", "--host", "0.0.0.0", "--port", "7199"]
