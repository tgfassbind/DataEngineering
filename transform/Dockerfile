# Pinned for reproducibility (same base image as the fetcher).
FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/transform

COPY requirements.txt .
# --no-deps + pip check: exactly the pinned set, and it must be consistent
RUN pip install --no-deps -r requirements.txt && pip check

COPY app ./app
# default line selection, shipped in the image; compose also mounts the repo's
# copy over it so edits to transform/config/lines.yml apply without a rebuild
COPY config ./config

RUN useradd --create-home --uid 10002 transform
USER transform

ENTRYPOINT ["python", "-m", "app"]
