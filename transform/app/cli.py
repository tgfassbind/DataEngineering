"""CLI entrypoint of the transform container.

  1. read and validate the command-line arguments
  2. load configuration (env vars) and the selected lines (CLI > env > file)
  3. transform each requested day independently: read raw/ from the data
     lake, write processed/<day>_stop_events.parquet and _run_scope.parquet
  4. print one summary line per day and return an exit code

Loading into BigQuery is not part of this job: Kestra loads the processed
files and runs bigquery/sql/10_load_core.sql afterwards.

Exit codes:
  0  every requested day was transformed and written
  1  at least one day FAILED (e.g. raw file missing); other days still ran
  2  invalid arguments / configuration / bucket missing or not accessible
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from google.api_core import exceptions as gexc
from google.auth.exceptions import DefaultCredentialsError

from .config import Config, ConfigError, resolve_lines
from .pipeline import transform_day
from .read import RawFileInvalid
from .storage import ObjectStore, RawFileMissing

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
        description="Transform gs://GCS_BUCKET/raw/YYYY-MM-DD_istdaten.csv into "
                    "processed/YYYY-MM-DD_stop_events.parquet and _run_scope.parquet.")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--date", type=_iso, help="single day, YYYY-MM-DD")
    sel.add_argument("--start-date", type=_iso, help="range start (inclusive)")
    p.add_argument("--end-date", type=_iso, help="range end (inclusive)")
    p.add_argument("--lines", action="append", metavar="LINE:OPERATOR[,...]",
                   help="override the configured lines, e.g. --lines IR75:SBB,S1:SBB "
                        "(repeatable)")
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

    log.info("Run: %d day(s) %s..%s, lines=%s (from %s), bucket=%s, "
             "chunk_size=%d, max_abs_delay_s=%d, max_segment_duration_s=%d",
             len(days), days[0], days[-1], ",".join(map(str, lines)), lines_source,
             cfg.gcs_bucket, cfg.chunk_size, cfg.limits.max_abs_delay_s,
             cfg.limits.max_segment_duration_s)

    try:
        store = ObjectStore(cfg)
        store.check_bucket()
    except (ConfigError, DefaultCredentialsError) as e:
        log.error("Configuration error: %s", e)
        return 2

    ok = failed = 0
    for day in days:
        try:
            summary = transform_day(cfg, store, day, lines)
        except (RawFileMissing, RawFileInvalid) as e:
            failed += 1
            log.error("date=%s status=FAILED %s", day, e)
            continue
        except gexc.GoogleAPIError as e:                # storage error after retries
            failed += 1
            log.error("date=%s status=FAILED storage error (%s): %s",
                      day, type(e).__name__, e)
            continue
        except Exception as e:                          # unexpected: report, keep going
            failed += 1
            log.exception("date=%s status=FAILED %s: %s", day, type(e).__name__, e)
            continue
        ok += 1
        log.info("date=%s status=OK\n%s", day, summary.log_line())

    log.info("Summary: days=%d ok=%d failed=%d", len(days), ok, failed)
    return 1 if failed else 0
