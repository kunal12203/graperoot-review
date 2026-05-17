FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY webhook.py            /app/webhook.py
COPY review.py             /app/review.py
COPY graph_service.py      /app/graph_service.py
COPY graph_builder_v6.2.py /app/graph_builder_v6.2.py

EXPOSE 8080
CMD ["python", "webhook.py"]
