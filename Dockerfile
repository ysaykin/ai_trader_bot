# ---- ARM64-native Python image (fast on M1 Max) ----
FROM python:3.12-slim AS runtime

WORKDIR /app

# System deps for pandas/numpy C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy packaging metadata and source before installing the editable project.
COPY pyproject.toml requirements.txt ./
RUN python -c "import subprocess, sys, tomllib; dependencies = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *dependencies])"
COPY README.md .
COPY main.py .
COPY trade_logger.py .
COPY strategies/ strategies/
COPY ml/ ml/
COPY dashboard/broker_client.py dashboard/presentation.py dashboard/monitoring.py dashboard/positions.py dashboard/
COPY tools/ tools/
RUN pip install --no-cache-dir --no-deps -r requirements.txt

CMD ["python", "main.py"]

# The test image includes both execution and dashboard dependencies. Tests run
# against an isolated source copy with no network or runtime-record mounts.
FROM runtime AS test
COPY reflex_dashboard/requirements.txt requirements-reflex.txt
COPY backend/requirements.txt requirements-backend.txt
RUN pip install --no-cache-dir -e '.[dev]' \
    -r requirements-reflex.txt -r requirements-backend.txt
