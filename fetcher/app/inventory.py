"""List what is in object storage under raw/ (key, size, ETag).

    docker compose run --rm --entrypoint python fetcher -m app.inventory

Two teams can diff this output to check they ended up with the same data.
"""
from __future__ import annotations

import sys

from .config import RAW_PREFIX, Config, ConfigError
from .storage import ObjectStore


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    store = ObjectStore(cfg)
    paginator = store.client.get_paginator("list_objects_v2")
    count = total = 0
    print(f"{'key':<34} {'bytes':>14}  etag")
    for page in paginator.paginate(Bucket=cfg.s3_bucket, Prefix=RAW_PREFIX):
        for obj in page.get("Contents", []):
            print(f"{obj['Key']:<34} {obj['Size']:>14}  {obj['ETag'].strip(chr(34))}")
            count += 1
            total += obj["Size"]
    print(f"{count} object(s), {total} bytes in s3://{cfg.s3_bucket}/{RAW_PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
