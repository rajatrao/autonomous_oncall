# Reference demo services (payment-api, banks, fraud, platform-control, chaos, alerts, Slack sim, dashboard).
# Not part of the product. One image; compose selects the service with its command.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install "fastapi>=0.110" "uvicorn>=0.29" "httpx>=0.27" "redis>=5" "prometheus-client>=0.20" "psycopg[binary]>=3.1" \
    && useradd --system --uid 10002 demo
WORKDIR /app
COPY . .
USER 10002
EXPOSE 8000
CMD ["uvicorn", "payment_api:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
