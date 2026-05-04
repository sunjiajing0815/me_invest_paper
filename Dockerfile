FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY templates/ ./templates/

RUN uv sync --frozen --no-dev

RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app

USER appuser

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "src.investor.main:app", "--host", "0.0.0.0", "--port", "8000"]
