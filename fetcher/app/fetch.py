"""Fetch pipeline: idempotency check -> route -> download -> upload.

For each requested date the Fetcher:
  1. checks whether raw/YYYY-MM-DD_istdaten.csv already exists in storage
  2. skips it, or (if missing / --force-overwrite) downloads it from the
     source chosen by routing.route()
  3. checks that the content looks like an Ist-Daten CSV
  4. uploads it to object storage
and reports one Result per date. A failing date never stops the others.
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import BinaryIO, Iterable, Iterator

import requests

from .config import Config, raw_key
from .routing import Source, group_by_month, route
from .storage import ObjectStore

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
USER_AGENT = "deng-datapipeline-fetcher/1.0"
# Member names inside the monthly ZIP: "YYYY-MM-DD_istdaten.csv" at the ZIP
# root, no sub-folders. Matched strictly so
# a format change upstream fails loudly instead of silently picking odd files.
MEMBER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_IstDaten\.csv$")
EXPECTED_HEADER_TOKEN = b"BETRIEBSTAG"


class Status(str, Enum):
    """Outcome of one date. Printed in the logs and counted in the summary."""
    FETCHED = "fetched" # was missing, now stored
    SKIPPED = "skipped" # already in storage, no --force-overwrite
    OVERWRITTEN = "overwritten" # already in storage, --force-overwrite
    FAILED = "failed" # error; the old object (if any) is untouched


@dataclass
class Result:
    """What happened to one date: source used, status, and a detail text
    (byte count and key on success, the error message on failure)."""
    day: date
    source: Source
    status: Status
    detail: str = ""


class ValidatingStream(io.RawIOBase):
    """Read-only file object over byte chunks.

    Checks the first bytes look like an Ist-Daten CSV *before* anything is
    uploaded and counts bytes for the log line.
    """

    def __init__(self, chunks: Iterable[bytes], label: str):
        """Read the first ~4 KB and validate them; raise ValueError if wrong."""
        self._it: Iterator[bytes] = iter(chunks)
        self._buf = b""
        self.bytes_read = 0
        head = b""
        while len(head) < 4096:
            try:
                chunk = next(self._it)
            except StopIteration:
                break
            head += chunk
        first_line = head.split(b"\n", 1)[0].upper()
        if not head:
            raise ValueError(f"{label}: empty file")
        if EXPECTED_HEADER_TOKEN not in first_line:
            raise ValueError(
                f"{label}: content does not look like an Ist-Daten CSV "
                f"(first bytes: {head[:80]!r})")
        self._buf = head

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
        self.bytes_read += n
        return n


def _file_chunks(f: BinaryIO) -> Iterator[bytes]:
    while chunk := f.read(CHUNK):
        yield chunk


class Fetcher:
    """Processes a list of dates: decides, downloads, uploads, reports."""

    def __init__(self, cfg: Config, store: ObjectStore, force_overwrite: bool):
        self.cfg = cfg
        self.store = store
        self.force = force_overwrite
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT

    # -- public ---------------------------------------------------------------
    def run(self, days: list[date], today: date) -> list[Result]:
        """Process all requested days; return one Result per day, sorted by date.

        Two phases: first check every date against storage (cheap, no
        downloads), then download only what is actually needed. That way a
        monthly ZIP is not downloaded at all if all its requested days exist.
        """
        results: dict[date, Result] = {}
        pending_permalink: list[date] = []
        pending_archive: list[date] = []
        existed: set[date] = set()

        # 1) Idempotency check for every requested date.
        for d in sorted(days):
            src = route(d, today)
            key = raw_key(d)
            try:
                present = self.store.exists(key)
            except Exception as e:  # storage error for this key only
                results[d] = Result(d, src, Status.FAILED, f"existence check: {e}")
                self._log(results[d])
                continue
            if present and not self.force:
                results[d] = Result(d, src, Status.SKIPPED, f"{key} already exists")
                self._log(results[d])
                continue
            if present:
                existed.add(d)
            (pending_permalink if src is Source.PERMALINK else pending_archive).append(d)

        # 2) Fetch what is still needed: archive months first (always older),
        #    then permalink days, so logs read roughly chronologically.
        for (year, month), month_days in group_by_month(pending_archive).items():
            for r in self._fetch_archive_month(year, month, month_days, existed):
                results[r.day] = r
                self._log(r)
        for d in pending_permalink:
            results[d] = self._fetch_permalink(d, d in existed)
            self._log(results[d])

        return [results[d] for d in sorted(results)]

    # -- permalink: one CSV, streamed straight into object storage ------------
    def _fetch_permalink(self, d: date, overwrite: bool) -> Result:
        """Download one day's CSV via the permalink and store it.

        The download is passed straight into the upload while it arrives
        (stream=True): no temp file
        """
        url = self.cfg.permalink_url_template.format(day=d.isoformat())
        key = raw_key(d)
        try:
            with self.http.get(url, stream=True, timeout=self.cfg.http_timeout_s) as r:
                r.raise_for_status()
                stream = ValidatingStream(r.iter_content(CHUNK), url)
                self.store.upload(key, io.BufferedReader(stream, CHUNK))
            status = Status.OVERWRITTEN if overwrite else Status.FETCHED
            return Result(d, Source.PERMALINK, status,
                          f"{stream.bytes_read} bytes -> {key}")
        except Exception as e:
            return Result(d, Source.PERMALINK, Status.FAILED, f"{url}: {e}")

    # -- archive: download month ZIP to temp dir, upload requested days -------
    def _fetch_archive_month(self, year: int, month: int, days: list[date],
                             existed: set[date]) -> list[Result]:
        """Download one monthly ZIP and store the requested days from it.

        Unlike the permalink, the ZIP must be on disk completely before it
        can be opened: a ZIP's table of contents is at the END of the file.
        It goes into a temporary directory on the scratch volume, which is
        deleted when this method ends, whether it succeeded or failed.

        If the download fails, every requested day of that month is FAILED.
        If one day is missing or broken inside the ZIP, only that day fails.
        """
        url = self.cfg.archive_url_template.format(year=f"{year:04d}", month=f"{month:02d}")
        os.makedirs(self.cfg.tmp_dir, exist_ok=True)
        # TemporaryDirectory is removed on success AND on failure.
        with tempfile.TemporaryDirectory(dir=self.cfg.tmp_dir,
                                         prefix=f"archive-{year:04d}-{month:02d}-") as tmp:
            zip_path = os.path.join(tmp, "archive.zip")

            # Step 1: download the ZIP to disk, piece by piece.
            try:
                log.info("Downloading archive %s -> %s", url, zip_path)
                size = 0
                with self.http.get(url, stream=True, timeout=self.cfg.http_timeout_s) as r:
                    r.raise_for_status()
                    with open(zip_path, "wb") as f:
                        for chunk in r.iter_content(CHUNK):
                            f.write(chunk)
                            size += len(chunk)
                log.info("Downloaded %d bytes; opening archive", size)
                zf = zipfile.ZipFile(zip_path)
            except Exception as e:
                return [Result(d, Source.ARCHIVE, Status.FAILED, f"archive {url}: {e}")
                        for d in days]

            with zf:
                # Step 2: index the ZIP's contents by date. Entries that do
                # not match the naming convention are ignored.
                members: dict[date, zipfile.ZipInfo] = {}
                for info in zf.infolist():
                    m = MEMBER_RE.match(info.filename)
                    if m and not info.is_dir():
                        members[date.fromisoformat(m.group(1))] = info

                # Step 3: for each requested day, decompress its CSV and pass
                # it straight into the upload.
                results = []
                for d in days:
                    key = raw_key(d)
                    info = members.get(d)
                    if info is None:
                        results.append(Result(d, Source.ARCHIVE, Status.FAILED,
                                              f"{d} not found in archive {url}"))
                        continue
                    try:
                        with zf.open(info) as member:  # pure-Python extraction
                            stream = ValidatingStream(_file_chunks(member), info.filename)
                            self.store.upload(key, io.BufferedReader(stream, CHUNK))
                        status = Status.OVERWRITTEN if d in existed else Status.FETCHED
                        results.append(Result(d, Source.ARCHIVE, status,
                                              f"{stream.bytes_read} bytes from "
                                              f"{info.filename} -> {key}"))
                    except Exception as e:
                        results.append(Result(d, Source.ARCHIVE, Status.FAILED,
                                              f"{info.filename}: {e}"))
                return results

    @staticmethod
    def _log(r: Result) -> None:
        """Write the one log line for a date: INFO on success/skip, ERROR on failure."""
        level = logging.ERROR if r.status is Status.FAILED else logging.INFO
        log.log(level, "date=%s source=%s status=%s %s",
                r.day.isoformat(), r.source.value, r.status.value.upper(), r.detail)
