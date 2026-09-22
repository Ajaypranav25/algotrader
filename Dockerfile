FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

COPY config ./config
COPY frontend ./frontend

RUN useradd --create-home --uid 10001 trader && mkdir -p data logs && chown -R trader data logs
USER trader

# Secrets come from the environment (docker run --env-file .env), never the image.
ENV HOST=0.0.0.0 LOG_FORMAT=json
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1
CMD ["algotrader", "serve"]
