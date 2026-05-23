FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir -e .

# Copy source
COPY src/       src/
COPY config/    config/
COPY scripts/   scripts/

# Runtime directories (mounted as volumes in production)
RUN mkdir -p data logs

CMD ["python", "-m", "src.main", "--live", "--config", "config/b97_micro_live.yaml"]
