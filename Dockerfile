FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INVESTIGATION_DB=/app/data/investigations.sqlite3

WORKDIR /app

RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY pyproject.toml ./
RUN python -m pip install --upgrade pip setuptools \
    && python -m pip install "fastapi>=0.115" "gradio>=6.0,<7.0" "uvicorn[standard]>=0.34"

COPY src ./src
COPY data/evaluation_cases.json ./data/evaluation_cases.json

RUN python -m pip install --no-deps --no-build-isolation .

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

CMD ["uvicorn", "mlops_investigator.api:app", "--host", "0.0.0.0", "--port", "8000"]
