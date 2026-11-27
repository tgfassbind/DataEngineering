"""Access to the data lake bucket in Google Cloud Storage.

The transform reads raw/<day>_istdaten.csv and writes its result to
processed/. Authentication: the Google client library finds the pipeline
service account's JSON key through GOOGLE_APPLICATION_CREDENTIALS. For tests,
STORAGE_EMULATOR_HOST points the same client at a local GCS emulator.

The bucket is created by Terraform; the transform never creates it.
"""
from __future__ import annotations

import io
import logging
from datetime import date
from typing import BinaryIO

from google.api_core import exceptions as gexc
from google.api_core.retry import Retry
from google.cloud import storage

from .config import Config, ConfigError, raw_key

log = logging.getLogger(__name__)

# Read the raw file in 8 MiB ranges: the stream stays sequential and only one
# range is held in memory.
READ_CHUNK_SIZE = 8 * 1024 * 1024
# Retry transient errors (429, 5xx, connection resets) for up to 5 minutes.
RETRY = Retry(initial=1.0, maximum=30.0, multiplier=2.0, timeout=300.0)


class RawFileMissing(Exception):
    """raw/<date>_istdaten.csv does not exist: the day was not fetched yet."""


class ObjectStore:
    """Access to one bucket in Cloud Storage."""

    def __init__(self, cfg: Config):
        self.bucket_name = cfg.gcs_bucket
        self.client = storage.Client(project=cfg.gcp_project)
        self.bucket = self.client.bucket(cfg.gcs_bucket)

    def check_bucket(self) -> None:
        """Fail early (exit code 2) if the bucket is missing or not accessible."""
        try:
            self.client.get_bucket(self.bucket_name, retry=RETRY)
        except gexc.NotFound:
            raise ConfigError(
                f"Bucket '{self.bucket_name}' does not exist. It is created by "
                "Terraform (terraform/), check GCS_BUCKET.") from None
        except gexc.Forbidden as e:
            raise ConfigError(
                f"No access to bucket '{self.bucket_name}': {e.message}. The pipeline "
                "service account needs Storage Bucket Viewer and Storage Object User.") from None

    def open_raw(self, day: date) -> tuple[BinaryIO, int]:
        """Open one day's raw file as a sequential binary stream.

        Returns the stream and the object size. The read is pinned to the
        object generation found here, so a fetcher overwriting the file at the
        same time cannot mix two versions into one read.
        """
        key = raw_key(day)
        blob = self.bucket.get_blob(key, retry=RETRY)
        if blob is None:
            raise RawFileMissing(
                f"gs://{self.bucket_name}/{key} not found - fetch the day first "
                f"(docker compose run --rm fetcher --date {day.isoformat()})")
        return blob.open("rb", chunk_size=READ_CHUNK_SIZE, retry=RETRY), blob.size

    def write(self, key: str, data: bytes, content_type: str) -> None:
        """Store data under key, replacing an existing object in one step."""
        self.bucket.blob(key).upload_from_file(
            io.BytesIO(data), size=len(data), content_type=content_type, retry=RETRY)
