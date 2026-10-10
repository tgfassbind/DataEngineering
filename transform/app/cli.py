"""CLI entrypoint of the transform container.

  1. read and validate the command-line arguments
  2. load configuration (env vars) and the selected lines (CLI > env > file)
  3. transform each requested day independently (one DB transaction per day)
  4. print one summary line per day and return an exit code

Exit codes:
  0  every requested day was transformed and loaded
  1  at least one day FAILED (e.g. raw file missing); other days still ran
  2  invalid arguments / configuration / storage or database unreachable
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

import psycopg
from botocore.exceptions import BotoCoreError, ClientError

from .config import Config, ConfigError, resolve_lines
from .pipeline import transform_day
from .read import (BucketUnavailable, RawFileInvalid, RawFileMissing, make_s3_client,
                   wait_for_bucket)

log = logging.getLogger("transform")
MAX_RANGE_DAYS = 3660   # about 10 years; guards against typos like 2062 for 2026


def _iso(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got '{value}'")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="transform",
        description="Transform raw/YYYY-MM-DD_istdaten.csv from object storage "
                    "into PostgreSQL staging.stop_events.")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--date", type=_iso, help="single day, YYYY-MM-DD")
    sel.add_argument("--start-date", type=_iso, help="range start (inclusive)")
    p.add_argument("--end-date", type=_iso, help="range end (inclusive)")
    p.add_argument("--lines", action="append", metavar="LINE:OPERATOR[,...]",
                   help="override the configured lines, e.g. --lines IR75:SBB,S1:SBB "
                        "(repeatable)")
    p.add_argument("--retransform", action="store_true",
                   help="delete the day's staging rows of the processed lines first, "
                        "then load fresh (same transaction)")
    return p


def parse_days(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[date]:
    if args.date:
        if args.end_date:
            parser.error("--end-date can only be used with --start-date")
        return [args.date]
    if not args.end_date:
        parser.error("--start-date requires --end-date")
    if args.end_date < args.start_date:
        parser.error(f"--end-date {args.end_date} is before --start-date {args.start_date}")
    n = (args.end_date - args.start_date).days + 1
    if n > MAX_RANGE_DAYS:
        parser.error(f"range of {n} days is too large (max {MAX_RANGE_DAYS})")
    return [args.start_date + timedelta(days=i) for i in range(n)]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    days = parse_days(args, parser)
    try:
        cfg = Config.from_env()
        lines, lines_source = resolve_lines(args.lines)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        return 2

    log.info("Run: %d day(s) %s..%s, lines=%s (from %s), retransform=%s, bucket=%s, "
             "db=%s@%s:%s/%s, chunk_size=%d, max_abs_delay_s=%d, max_segment_duration_s=%d",
             len(days), days[0], days[-1], ",".join(map(str, lines)), lines_source,
             args.retransform, cfg.s3_bucket, cfg.pg_user, cfg.pg_host, cfg.pg_port,
             cfg.pg_db, cfg.chunk_size, cfg.limits.max_abs_delay_s,
             cfg.limits.max_segment_duration_s)

    s3 = make_s3_client(cfg)
    try:
        wait_for_bucket(s3, cfg.s3_bucket, cfg.s3_bucket_wait_s)
    except (BucketUnavailable, ClientError, BotoCoreError) as e:
        log.error("Object storage / bucket '%s' not reachable: %s", cfg.s3_bucket, e)
        return 2
    try:
        conn = psycopg.connect(**cfg.pg_conninfo(), connect_timeout=15, autocommit=True)
    except psycopg.OperationalError as e:
        log.error("PostgreSQL not reachable: %s", e)
        return 2

    ok = failed = 0
    with conn:
        for day in days:
            try:
                summary = transform_day(cfg, s3, conn, day, lines, args.retransform)
            except (RawFileMissing, RawFileInvalid) as e:
                failed += 1
                log.error("date=%s status=FAILED %s", day, e)
                continue
            except psycopg.Error as e:
                if conn.closed or conn.broken:          # connection lost: stop the run
                    failed = len(days) - ok
                    log.error("date=%s status=FAILED database connection lost, "
                              "remaining days not run: %s", day, e)
                    break
                failed += 1                             # e.g. deadlock, lock timeout
                log.error("date=%s status=FAILED database error (%s): %s",
                          day, type(e).__name__, e)
                continue
            except Exception as e:                      # unexpected: report, keep going
                failed += 1
                log.exception("date=%s status=FAILED %s: %s", day, type(e).__name__, e)
                continue
            ok += 1
            log.info("date=%s status=OK\n%s", day, summary.log_line())

    log.info("Summary: days=%d ok=%d failed=%d", len(days), ok, failed)
    return 1 if failed else 0
