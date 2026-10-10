"""Read one day's raw CSV from object storage, chunk by chunk, and keep only
the configured lines.

Memory stays bounded: the S3 object is streamed (boto3 StreamingBody) straight
into pandas.read_csv(chunksize=...). Nothing is written to disk and at no point
is the whole file in memory: only one chunk of raw rows plus the (small)
already-filtered rows of the selected lines.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import warnings
from dataclasses import dataclass
from datetime import date

import boto3
import pandas as pd
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError

from .config import Config, Line, raw_key

log = logging.getLogger(__name__)

# Columns the transform needs (everything is read as text, never inferred).
USECOLS = [
    "BETRIEBSTAG", "FAHRT_BEZEICHNER", "BETREIBER_ABK", "PRODUKT_ID",
    "LINIEN_TEXT", "ZUSATZFAHRT_TF", "FAELLT_AUS_TF", "BPUIC",
    "HALTESTELLEN_NAME", "ANKUNFTSZEIT", "AN_PROGNOSE", "AN_PROGNOSE_STATUS",
    "ABFAHRTSZEIT", "AB_PROGNOSE", "AB_PROGNOSE_STATUS", "DURCHFAHRT_TF",
]
# The Ist-Daten v2 files are comma separated; the fetcher README documents
# ';' (format of the older files). Both are accepted, decided from the header.
ACCEPTED_SEPARATORS = (",", ";")
MAX_HEADER_BYTES = 64 * 1024
STREAM_BLOCK = 1024 * 1024


class RawFileMissing(Exception):
    """raw/<date>_istdaten.csv does not exist: the day was not fetched yet."""


class RawFileInvalid(Exception):
    """The object exists but does not match the Ist-Daten format."""


@dataclass
class ReadResult:
    frame: pd.DataFrame      # rows of the selected lines, USECOLS, all str
    rows_read: int           # data rows in the whole file
    rows_after_filter: int
    object_bytes: int
    separator: str
    trips_excluded_by_via: int = 0


def make_s3_client(cfg: Config):
    """Same client settings as the fetcher (path-style works locally and on AWS)."""
    return boto3.client(
        "s3",
        endpoint_url=cfg.s3_endpoint_url,
        aws_access_key_id=cfg.s3_access_key_id,
        aws_secret_access_key=cfg.s3_secret_access_key,
        region_name=cfg.s3_region,
        config=BotoConfig(s3={"addressing_style": "path"},
                          retries={"max_attempts": 5, "mode": "standard"}),
    )


class BucketUnavailable(Exception):
    """Storage not reachable or bucket missing after waiting."""


def wait_for_bucket(s3, bucket: str, wait_s: int) -> None:
    """Wait until the bucket exists. The storage service reports healthy
    before it has created S3_BUCKET, so the very first job on a fresh clone
    may arrive a moment too early (same behaviour as the fetcher)."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            s3.head_bucket(Bucket=bucket)
            return
        except (ClientError, EndpointConnectionError) as e:
            code = (e.response.get("Error", {}).get("Code", "")
                    if isinstance(e, ClientError) else "unreachable")
            if code not in {"404", "NoSuchBucket", "NotFound", "unreachable"}:
                raise BucketUnavailable(f"bucket '{bucket}': {e}") from None
            if time.monotonic() > deadline:
                raise BucketUnavailable(
                    f"bucket '{bucket}' not available after {wait_s}s ({code})") from None
            log.info("Bucket '%s' not available yet (%s), waiting ...", bucket, code)
            time.sleep(2)


class _PrefixedStream(io.RawIOBase):
    """Read-only stream: already-consumed bytes (the header) followed by the
    rest of the underlying stream. Lets us inspect the header line without
    seeking (an S3 stream cannot seek)."""

    def __init__(self, prefix: bytes, rest):
        self._buf = prefix
        self._rest = rest

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if not self._buf:
            self._buf = self._rest.read(len(b))
            if not self._buf:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def _split_header(stream) -> tuple[bytes, str]:
    """Consume bytes up to and including the first newline; return them and
    the decoded header line."""
    head = b""
    while b"\n" not in head:
        block = stream.read(4096)
        if not block:
            break
        head += block
        if len(head) > MAX_HEADER_BYTES:
            raise RawFileInvalid("no line break in the first 64 KB: not a CSV")
    if not head:
        raise RawFileInvalid("object is empty")
    first = head.split(b"\n", 1)[0].rstrip(b"\r")
    try:
        line = first.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise RawFileInvalid(f"header is not UTF-8: {e}") from None
    return head, line


FLAG_COLUMNS = ("ZUSATZFAHRT_TF", "FAELLT_AUS_TF", "DURCHFAHRT_TF")
FLAG_VALUES = {"true", "false"}


def detect_separator(header: str) -> str:
    """Pick the separator whose split yields all required columns
    (parsed with the csv module, i.e. the same quoting rules as the data)."""
    for sep in ACCEPTED_SEPARATORS:
        cols = [c.strip() for c in next(csv.reader([header], delimiter=sep), [])]
        if cols and cols[0] == "BETRIEBSTAG":
            missing = [c for c in USECOLS if c not in cols]
            if missing:
                raise RawFileInvalid(f"header lacks required columns: {missing}")
            return sep
    raise RawFileInvalid(
        f"header does not start with BETRIEBSTAG using ',' or ';': {header[:120]!r}")


def filter_chunks(binary_stream, lines: list[Line], chunk_size: int) -> ReadResult:
    """Core of the reader, independent of S3 (tests feed a local file)."""
    head, header = _split_header(binary_stream)
    sep = detect_separator(header)
    stream = io.BufferedReader(_PrefixedStream(head, binary_stream), STREAM_BLOCK)

    wanted = {l.scope for l in lines}
    wanted_keys = {f"{t}\x1f{o}" for t, o in wanted}
    kept: list[pd.DataFrame] = []
    rows_read = 0
    # All columns are read (not usecols): only then does the C parser reject
    # rows with too many fields instead of silently shifting values.
    reader = pd.read_csv(
        stream, sep=sep, dtype=str, encoding="utf-8-sig",
        keep_default_na=False, na_filter=False,   # empty field -> "" (never NaN)
        index_col=False,   # never guess an index column from an over-long first row
        chunksize=chunk_size, engine="c")
    try:
        with reader, warnings.catch_warnings():
            # with index_col=False an over-long FIRST row is only a warning
            warnings.simplefilter("error", pd.errors.ParserWarning)
            for chunk in reader:
                rows_read += len(chunk)
                key = (chunk["LINIEN_TEXT"].str.strip() + "\x1f"
                       + chunk["BETREIBER_ABK"].str.strip())
                sel = chunk.loc[key.isin(wanted_keys), USECOLS]
                if len(sel):
                    kept.append(sel.copy())   # copy: do not keep the whole chunk alive
                del chunk, key, sel
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError,
            pd.errors.ParserWarning) as e:
        raise RawFileInvalid(
            f"malformed CSV after about {rows_read} data rows: {e}") from None
    frame = (pd.concat(kept, ignore_index=True) if kept
             else pd.DataFrame({c: pd.Series(dtype=str) for c in USECOLS}))
    frame = frame[USECOLS]
    _check_flags(frame)
    frame, excluded_trips = apply_via_filter(frame, lines)
    return ReadResult(frame=frame, rows_read=rows_read, rows_after_filter=len(frame),
                      object_bytes=-1, separator=sep, trips_excluded_by_via=excluded_trips)

def apply_via_filter(frame: pd.DataFrame, lines: list[Line]) -> tuple[pd.DataFrame, int]:
    """Keep a trip of LINIEN_TEXT+BETREIBER_ABK if one configured entry for that
    pair has no via stops, or the trip has a row (stop or pass-through) at one
    of the entry's via stops. Whole trips are kept or dropped, never single
    stops. Runs on the already line-filtered rows, so it is cheap."""
    if not any(l.via_bpuic for l in lines) or frame.empty:
        return frame, 0
    text = frame["LINIEN_TEXT"].str.strip()
    operator = frame["BETREIBER_ABK"].str.strip()
    trip = frame["FAHRT_BEZEICHNER"].str.strip()
    bpuic = frame["BPUIC"].str.strip()
    keep = pd.Series(False, index=frame.index)
    for scope in {l.scope for l in lines}:
        in_scope = (text == scope[0]) & (operator == scope[1])
        entries = [l for l in lines if l.scope == scope]
        if any(not l.via_bpuic for l in entries):
            keep |= in_scope                              # unrestricted entry wins
            continue
        stops = {str(b) for l in entries for b in l.via_bpuic}
        trips_ok = set(trip[in_scope & bpuic.isin(stops)])
        keep |= in_scope & trip.isin(trips_ok)
    excluded = trip[~keep].nunique()
    return frame.loc[keep].reset_index(drop=True), int(excluded)

def read_day(s3, bucket: str, day: date, lines: list[Line], chunk_size: int) -> ReadResult:
    """Stream raw/<day>_istdaten.csv from object storage and filter it."""
    key = raw_key(day)
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise RawFileMissing(
                f"s3://{bucket}/{key} not found - fetch the day first "
                f"(docker compose run --rm fetcher --date {day.isoformat()})") from None
        raise
    body = obj["Body"]
    try:
        result = filter_chunks(body, lines, chunk_size)
    finally:
        body.close()
    result.object_bytes = int(obj.get("ContentLength", -1))
    _check_operating_day(result.frame, day, key)
    return result


def _check_flags(frame: pd.DataFrame) -> None:
    """Rows with too FEW fields are padded with "" by the parser; a shifted or
    truncated row shows up as a flag column that is not true/false."""
    for col in FLAG_COLUMNS:
        bad = frame.loc[~frame[col].str.strip().str.lower().isin(FLAG_VALUES), col]
        if len(bad):
            raise RawFileInvalid(
                f"{len(bad)} rows of the selected lines have {col} not in true/false "
                f"(e.g. {bad.iloc[0]!r}): truncated or shifted rows")


def _check_operating_day(frame: pd.DataFrame, day: date, key: str) -> None:
    """Every kept row must belong to the day the file is named after."""
    expected = day.strftime("%d.%m.%Y")
    bad = frame.loc[frame["BETRIEBSTAG"].str.strip() != expected, "BETRIEBSTAG"]
    if len(bad):
        raise RawFileInvalid(
            f"{key}: {len(bad)} rows have BETRIEBSTAG != {expected} "
            f"(e.g. {bad.iloc[0]!r}); file and date do not match")
