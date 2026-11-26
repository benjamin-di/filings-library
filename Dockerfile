FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        qpdf \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==0.13.8"

RUN useradd --uid 1001 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --locked --no-dev --extra extractor \
    && mkdir -p /data /scratch \
    && chown -R 1001:1001 /app /data /scratch

USER 1001

ENV FILINGS_DATA_DIR=/data

EXPOSE 8080 8081

ENTRYPOINT ["filings-library"]
CMD ["serve"]
