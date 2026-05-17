FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY review.py          /app/review.py
COPY webhook.py         /app/webhook.py
COPY graph_service.py   /app/graph_service.py
COPY graph_builder_v6.2.py /app/graph_builder_v6.2.py
COPY landing/           /app/landing/

# Required env vars (set in Railway / docker run -e):
#   ANTHROPIC_API_KEY       — Claude API key
#   GITHUB_WEBHOOK_SECRET   — from GitHub App settings
#   GITHUB_APP_ID           — numeric App ID
#   GITHUB_PRIVATE_KEY      — RSA private key PEM (newlines as \n)
#
# Self-hosted / PAT fallback:
#   GITHUB_TOKEN            — personal access token (if not using GitHub App)

EXPOSE 8080
CMD ["python", "webhook.py"]
