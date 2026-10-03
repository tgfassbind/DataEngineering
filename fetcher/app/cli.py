"""CLI entrypoint of the fetcher container.

This module only handles the "outside" of a run:
  1. read and validate the command-line arguments
  2. load the configuration from environment variables
  3. hand the list of dates to the Fetcher (app/fetch.py), which does the work
  4. print a summary and return an exit code

Exit codes (for the orchestrator):
  0  every requested date is FETCHED, OVERWRITTEN or SKIPPED
  1  at least one date FAILED
  2  invalid arguments / configuration
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date

from .config import Config, ConfigError
from .fetch import Fetcher, Status
from .routing import expand_range, route, today_in
from .storage import ObjectStore

log = logging.getLogger("fetcher")

def _iso(value: str) -> date:
    """Convert one CLI argument like "2026-10-01" into a date object.

    argparse calls this for --date, --start-date and --end-date. A wrong format
    (e.g. "01.10.2026") raises ArgumentTypeError, which argparse turns into a
    readable error message and exit code 2.
    """
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got '{value}'")


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface.

    --date and --start-date are mutually exclusive and one of them is required:
    a run is either a single day or a range.
    --end-date belongs to --start-date; that pairing is checked in parse_days(),
    --force-overwrite can be combined with both.
    """
    p = argparse.ArgumentParser(
        prog="fetcher",
        description="Fetch SBB Ist-Daten v2 daily CSVs into object storage "
                    "(raw/YYYY-MM-DD_istdaten.csv).")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--date", type=_iso, help="single day, YYYY-MM-DD")
    sel.add_argument("--start-date", type=_iso, help="range start (inclusive)")
    p.add_argument("--end-date", type=_iso, help="range end (inclusive)")
    p.add_argument("--force-overwrite", action="store_true",
                   help="re-fetch and overwrite even if the object already exists")
    return p


def parse_days(args: argparse.Namespace, parser: argparse.ArgumentParser,
               today: date) -> list[date]:
    """Turn the parsed arguments into the list of days to process.

    - --date X -> [X]
    - --start-date A --end-date B -> every day from A to B, both included

    Rejects invalid combinations (--end-date without --start-date and vice
    versa, end before start) and any day after "today". parser.error() prints
    the message and exits with code 2, before anything is downloaded.
    """
    if args.date:
        if args.end_date:
            parser.error("--end-date can only be used with --start-date")
        days = [args.date]
    else:
        if not args.end_date:
            parser.error("--start-date requires --end-date")
        try:
            days = expand_range(args.start_date, args.end_date)
        except ValueError as e: # End date before start date
            parser.error(str(e))

    # DAta for future days does not exist yet. Fail early instead of per date.
    future = [d for d in days if d > today]
    if future:
        parser.error(f"date(s) after today ({today}) requested, first: {future[0]}")
    for d in days:
        route(d, today)  # safety net
    return days


def main(argv: list[str] | None = None) -> int:
    """Run the fetcher once and return its exit code.

    argv: the command-line arguments; None means "use sys.argv" (normal case).
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = Config.from_env()
        today = today_in(cfg.timezone, cfg.reference_date)
    except (ConfigError, ValueError, KeyError) as e:  # KeyError: unknown timezone
        log.error("Configuration error: %s", e)
        return 2
    days = parse_days(args, parser, today)

    log.info("Run: %d date(s) %s..%s, today=%s (%s%s), force_overwrite=%s, bucket=%s",
             len(days), days[0], days[-1], today, cfg.timezone,
             ", pinned via FETCHER_REFERENCE_DATE" if cfg.reference_date else "",
             args.force_overwrite, cfg.s3_bucket)

    store = ObjectStore(cfg)
    try:
        store.ensure_bucket(create=cfg.s3_create_bucket, wait_s=cfg.s3_bucket_wait_s)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        return 2
    results = Fetcher(cfg, store, args.force_overwrite).run(days, today)

    counts = Counter(r.status for r in results)
    log.info("Summary: %s", ", ".join(f"{s.value}={counts.get(s, 0)}" for s in Status))
    return 1 if counts.get(Status.FAILED) else 0


if __name__ == "__main__":
    sys.exit(main())
