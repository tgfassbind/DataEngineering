"""List what is in the data lake under raw/ (name, size, MD5).

    docker compose run --rm --entrypoint python fetcher -m app.inventory

Two teams can diff this output to check they ended up with the same data.
"""
from __future__ import annotations

import base64
import sys

from .config import RAW_PREFIX, Config, ConfigError
from .storage import ObjectStore


def main() -> int:
    try:
        cfg = Config.from_env()
        store = ObjectStore(cfg)
        store.check_bucket()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    count = total = 0
    print(f"{'name':<34} {'bytes':>14}  md5")
    for blob in store.list(RAW_PREFIX):
        md5 = base64.b64decode(blob.md5_hash).hex() if blob.md5_hash else "-"
        print(f"{blob.name:<34} {blob.size:>14}  {md5}")
        count += 1
        total += blob.size
    print(f"{count} object(s), {total} bytes in gs://{cfg.gcs_bucket}/{RAW_PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
