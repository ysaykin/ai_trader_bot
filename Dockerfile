# ---- ARM64-native Python image (fast on M1 Max) ----
FROM python:3.12-slim

WORKDIR /app

# System deps for pandas/numpy C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy packaging metadata and source before installing the editable project.
COPY pyproject.toml requirements.txt README.md ./
COPY main.py .
COPY trade_logger.py .
COPY strategies/ strategies/
COPY ml/ ml/
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "main.py"]
