from __future__ import annotations

import logging
import time
from typing import BinaryIO

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError

from .config import Config, ConfigError

log = logging.getLogger(__name__)


class ObjectStore:
    """Access to one bucket in object storage."""
    
    def __init__(self, cfg: Config):
        """Create the S3 client from the configuration."""
        self.bucket = cfg.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.s3_endpoint_url,
            aws_access_key_id=cfg.s3_access_key_id,
            aws_secret_access_key=cfg.s3_secret_access_key,
            region_name=cfg.s3_region,
            # Path-style addressing works with local S3 servers and with AWS.
            config=BotoConfig(s3={"addressing_style": "path"},
                              retries={"max_attempts": 5, "mode": "standard"}),
        )

    def ensure_bucket(self, create: bool, wait_s: int = 60) -> None:
        """Wait until storage is reachable and the bucket exists."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                self.client.head_bucket(Bucket=self.bucket)
                return
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
                if create:
                    log.info("Creating bucket '%s'", self.bucket)
                    self.client.create_bucket(Bucket=self.bucket)
                    return
                if time.monotonic() > deadline:
                    raise ConfigError(
                        f"Bucket '{self.bucket}' does not exist (waited {wait_s}s; "
                        "S3_CREATE_BUCKET is false)")
                log.info("Bucket '%s' not there yet, waiting ...", self.bucket)
            except EndpointConnectionError:
                if time.monotonic() > deadline:
                    raise
                log.info("Object storage not reachable yet, retrying ...")
            time.sleep(2)

    def exists(self, key: str) -> bool:
        """Return True if an object with this key is already stored.

        This is the idempotency check: the fetcher calls it for every date
        before downloading anything. Uses HEAD object, which only reads the
        object's metadata.
        """
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def upload(self, key: str, fileobj: BinaryIO) -> None:
        """Store the content of fileobj under key, replacing any existing object."""
        self.client.upload_fileobj(
            fileobj, self.bucket, key, ExtraArgs={"ContentType": "text/csv"})

    def size(self, key: str) -> int:
        return self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
