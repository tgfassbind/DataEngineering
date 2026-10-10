"""Development / test helper: put a raw file into object storage without the fetcher.

Two modes (both write raw/<date>_istdaten.csv, the fetcher's storage contract):

  --file PATH         upload a local CSV unchanged (e.g. the 21.09.2026 fixture)
  --synthetic ROWS    generate a deterministic multi-line file of ROWS data rows
                      and stream it straight into storage (never on disk, never
                      fully in memory) - for the memory/chunking test

Refuses to overwrite an existing object unless --force is given, so it cannot
silently replace a day the fetcher stored.

    python -m app.seed --file dataset/excerpt_21092026_istdaten_v2.csv --date 2026-09-21
    python -m app.seed --synthetic 2000000 --date 2026-09-25
"""
from __future__ import annotations

import argparse
import io
import logging
import random
import sys
from datetime import date, datetime, timedelta
from typing import Iterator

from botocore.exceptions import ClientError

from .config import Config, ConfigError, raw_key
from .read import BucketUnavailable, make_s3_client, wait_for_bucket

log = logging.getLogger("seed")

HEADER = ("BETRIEBSTAG,FAHRT_BEZEICHNER,BETREIBER_ID,BETREIBER_ABK,BETREIBER_NAME,PRODUKT_ID,"
          "LINIEN_ID,LINIEN_TEXT,UMLAUF_ID,VERKEHRSMITTEL_TEXT,ZUSATZFAHRT_TF,FAELLT_AUS_TF,"
          "BPUIC,HALTESTELLEN_NAME,ANKUNFTSZEIT,AN_PROGNOSE,AN_PROGNOSE_STATUS,ABFAHRTSZEIT,"
          "AB_PROGNOSE,AB_PROGNOSE_STATUS,DURCHFAHRT_TF,SLOID\n")
STOPS_PER_TRIP = 10
# every 40th trip is IR75/SBB, the rest are other lines; S1 exists for two
# operators so the filter must use LINIEN_TEXT + BETREIBER_ABK
TARGET_EVERY = 40
OTHER_LINES = [("S1", "SBB"), ("S1", "BLS"), ("S3", "SBB"), ("IC5", "SBB"), ("RE", "SOB"),
               ("1", "VBZ"), ("2", "BVB"), ("S8", "SBB"), ("IR75", "THURBO"), ("31", "VBL")]


def synthetic_lines(rows: int, day: date, seed: int = 20260921) -> Iterator[bytes]:
    """Deterministic rows: same arguments -> byte-identical file."""
    rnd = random.Random(seed)
    d = day.strftime("%d.%m.%Y")
    yield HEADER.encode()
    produced = trip_no = 0
    while produced < rows:
        line, operator = (("IR75", "SBB") if trip_no % TARGET_EVERY == 0
                          else OTHER_LINES[trip_no % len(OTHER_LINES)])
        start = datetime(day.year, day.month, day.day, 5) + timedelta(minutes=trip_no % 1080)
        buf = []
        for s in range(STOPS_PER_TRIP):
            if produced >= rows:
                break
            planned = start + timedelta(minutes=6 * s)
            arr = "" if s == 0 else planned.strftime("%d.%m.%Y %H:%M")
            dep = "" if s == STOPS_PER_TRIP - 1 else (planned + timedelta(minutes=1)).strftime(
                "%d.%m.%Y %H:%M")
            delay = rnd.randint(-90, 400)
            arr_act = "" if not arr else (planned + timedelta(seconds=delay)).strftime(
                "%d.%m.%Y %H:%M:%S")
            dep_act = "" if not dep else (planned + timedelta(minutes=1, seconds=delay + 20)
                                          ).strftime("%d.%m.%Y %H:%M:%S")
            buf.append(
                f"{d},synthetic:{trip_no:07d},85:11,{operator},{operator} AG,Zug,{trip_no % 900},"
                f"{line},,X,false,false,{8500000 + s},Halt {s},{arr},{arr_act},"
                f"{'REAL' if arr else ''},{dep},{dep_act},{'REAL' if dep else ''},false,\n")
            produced += 1
        yield "".join(buf).encode()
        trip_no += 1


class _IterStream(io.RawIOBase):
    def __init__(self, it: Iterator[bytes]):
        self._it, self._buf, self.bytes = it, b"", 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf:
            try:
                self._buf = next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        self.bytes += n
        return n


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="seed", description=__doc__.split("\n\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="local CSV to upload unchanged")
    src.add_argument("--synthetic", type=int, metavar="ROWS", help="generate ROWS data rows")
    p.add_argument("--date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    p.add_argument("--force", action="store_true", help="overwrite an existing object")
    args = p.parse_args(argv)
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        return 2
    s3 = make_s3_client(cfg)
    try:
        wait_for_bucket(s3, cfg.s3_bucket, cfg.s3_bucket_wait_s)
    except BucketUnavailable as e:
        log.error("Object storage: %s", e)
        return 2
    key = raw_key(args.date)
    try:
        s3.head_object(Bucket=cfg.s3_bucket, Key=key)
        if not args.force:
            log.error("s3://%s/%s already exists; use --force to overwrite", cfg.s3_bucket, key)
            return 1
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            raise
    if args.file:
        with open(args.file, "rb") as f:
            s3.upload_fileobj(f, cfg.s3_bucket, key, ExtraArgs={"ContentType": "text/csv"})
        log.info("uploaded %s -> s3://%s/%s", args.file, cfg.s3_bucket, key)
    else:
        stream = _IterStream(synthetic_lines(args.synthetic, args.date))
        s3.upload_fileobj(io.BufferedReader(stream, 1024 * 1024), cfg.s3_bucket, key,
                          ExtraArgs={"ContentType": "text/csv"})
        target = sum(1 for t in range(0, -(-args.synthetic // STOPS_PER_TRIP))
                     if t % TARGET_EVERY == 0)
        log.info("generated %d rows (%d bytes, ~%d IR75/SBB trips) -> s3://%s/%s",
                 args.synthetic, stream.bytes, target, cfg.s3_bucket, key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
