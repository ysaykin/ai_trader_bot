# ---- ARM64-native Python image (fast on M1 Max) ----
FROM python:3.11-slim

WORKDIR /app

# System deps for pandas/numpy C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (cached layer — only rebuilds when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY trade_logger.py .
COPY strategies/ strategies/
COPY ml/ ml/

CMD ["python", "main.py"]
