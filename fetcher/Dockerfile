# Pinned for reproducibility.
FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/fetcher

COPY requirements.txt .
RUN pip install --no-deps -r requirements.txt && pip check \
 # Fail if timezone is missing
 && python -c "import zoneinfo; zoneinfo.ZoneInfo('Europe/Zurich')"

COPY app ./app

RUN useradd --create-home --uid 10001 fetcher \
 && mkdir -p /tmp/fetcher && chown fetcher:fetcher /tmp/fetcher
USER fetcher

# Temporary space for archive ZIPs. Gets deleted after the after the container is removed.
VOLUME ["/tmp/fetcher"]

ENTRYPOINT ["python", "-m", "app"]
