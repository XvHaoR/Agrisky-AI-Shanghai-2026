FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System dependencies used by GeoPandas/Rasterio wheels and spatial file readers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gdal-bin libgdal-dev fonts-noto-cjk fontconfig \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY api_gateway/requirements.txt /app/api_gateway/requirements.txt
COPY space_engine/requirements.txt /app/space_engine/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api_gateway/ /app/api_gateway/
COPY space_engine/ /app/space_engine/
COPY agent/ /app/agent/
COPY config/ /app/config/
COPY legal_corpus/ /app/legal_corpus/

ENV AGRISKY_DB_PATH=/app/data/agrisky.db
ENV AGRISKY_OUTPUT_ROOT=/app/outputs
ENV AGRISKY_RISK_RULES_PATH=/app/config/risk_rules.yaml
ENV AGRISKY_PAYOUT_RULES_PATH=/app/config/payout_rules.yaml
ENV AGRISKY_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
ENV AGRISKY_ALLOW_MOCK_REMOTE_SENSING=false

EXPOSE 8000

WORKDIR /app/api_gateway
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
