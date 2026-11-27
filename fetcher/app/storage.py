"""Access to the data lake bucket in Google Cloud Storage.

Authentication: the Google client library finds the pipeline service
account's JSON key through GOOGLE_APPLICATION_CREDENTIALS (mounted read-only
into the container). For tests, STORAGE_EMULATOR_HOST points the same client
at a local GCS emulator instead.

The bucket itself is created by Terraform; the fetcher never creates it.
"""
from __future__ import annotations

import logging
import shutil
from typing import BinaryIO, Iterator

from google.api_core import exceptions as gexc
from google.api_core.retry import Retry
from google.cloud import storage

from .config import Config, ConfigError

log = logging.getLogger(__name__)

# Upload in 16 MiB pieces (must be a multiple of 256 KiB). Only one piece is
# held in memory at a time, so a 650 MB daily file never sits in RAM.
UPLOAD_CHUNK_SIZE = 16 * 1024 * 1024
COPY_BUFFER = 1024 * 1024
# Retry transient errors (429, 5xx, connection resets) for up to 5 minutes.
# Safe for every call here: they either only read or replace a whole object.
RETRY = Retry(initial=1.0, maximum=30.0, multiplier=2.0, timeout=300.0)


class ObjectStore:
    """Access to one bucket in Cloud Storage."""

    def __init__(self, cfg: Config):
        self.bucket_name = cfg.gcs_bucket
        self.client = storage.Client(project=cfg.gcp_project)
        self.bucket = self.client.bucket(cfg.gcs_bucket)

    def check_bucket(self) -> None:
        """Fail early (exit code 2) if the bucket is missing or not accessible,
        before anything is downloaded."""
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

    def exists(self, key: str) -> bool:
        """Return True if an object with this key is already stored.

        This is the idempotency check: the fetcher calls it for every date
        before downloading anything. It only reads the object's metadata.
        """
        return self.bucket.blob(key).exists(retry=RETRY)

    def upload(self, key: str, fileobj: BinaryIO) -> None:
        """Store the content of fileobj under key, replacing any existing object.

        Streams in UPLOAD_CHUNK_SIZE pieces (resumable upload). The new object
        only becomes visible when the upload is complete: if reading fileobj
        fails half-way, the upload is cancelled and an existing object with
        the same key stays unchanged.
        """
        blob = self.bucket.blob(key)
        with blob.open("wb", chunk_size=UPLOAD_CHUNK_SIZE, content_type="text/csv",
                       retry=RETRY) as out:
            shutil.copyfileobj(fileobj, out, COPY_BUFFER)

    def size(self, key: str) -> int:
        blob = self.bucket.get_blob(key, retry=RETRY)
        if blob is None:
            raise FileNotFoundError(f"gs://{self.bucket_name}/{key}")
        return blob.size

    def list(self, prefix: str) -> Iterator[storage.Blob]:
        """All objects whose name starts with prefix, in name order."""
        return self.client.list_blobs(self.bucket_name, prefix=prefix, retry=RETRY)
