"""All runtime configuration comes from environment variables.

Nothing environment-specific is hardcoded: bucket, sources and routing are
set per environment (.env), the code stays the same.

This module also defines the storage key contract (raw/YYYY-MM-DD_istdaten.csv),
because it is the one place every other module imports from.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

# Shared storage contract: every daily file is stored here
# regardless of whether it came from the permalink or an archive ZIP.
RAW_PREFIX = "raw/"
RAW_KEY_TEMPLATE = RAW_PREFIX + "{day}_istdaten.csv"

# Fallback if the url templates have not been set in the .env file
# Daily CSV {day} -> YYYY-MM-DD
DEFAULT_PERMALINK_URL_TEMPLATE = (
    "https://data.opentransportdata.swiss/dataset/ist-daten-v2/"
    "resource_permalink/{day}_istdaten.csv"
)
# Monthly archive ZIP ({year} -> 2026, {month} -> 09)
DEFAULT_ARCHIVE_URL_TEMPLATE = (
    "https://archive.opentransportdata.swiss/istdaten/{year}/"
    "ist-daten-v2-{year}-{month}.zip"
)


class ConfigError(Exception):
    """Invalid or missing configuration -> CLI exit code 2."""


def raw_key(day: date) -> str:
    """Return the storage key for one day, e.g. "raw/2026-10-01_istdaten.csv"."""
    return RAW_KEY_TEMPLATE.format(day=day.isoformat())


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    """Read one environment variable as text.
    - unset or empty -> default
    - required and no value (and no default) -> ConfigError
    - surrounding spaces are stripped
    """
    value = (os.environ.get(name) or "").strip() or default
    if required and not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    """All settings of one fetcher run, read once at startup.

    frozen=True: the values cannot be changed after creation, so every part
    of the run is guaranteed to see the same configuration.
    """
    # Data lake (Cloud Storage). Credentials are not configured here: the
    # Google client reads GOOGLE_APPLICATION_CREDENTIALS itself.
    gcs_bucket: str
    gcp_project: str | None       # empty -> taken from the service-account key
    # Sources
    permalink_url_template: str
    archive_url_template: str
    http_timeout_s: int
    # Routing
    timezone: str                 # defines what "current month" means
    reference_date: date | None   # pin "today" for deterministic routing
    # Temp storage for archive ZIPs
    tmp_dir: str

    @classmethod
    def from_env(cls) -> "Config":
        ref = _env("FETCHER_REFERENCE_DATE")
        try:
            ref_date = date.fromisoformat(ref) if ref else None
        except ValueError:
            raise ConfigError(f"FETCHER_REFERENCE_DATE must be YYYY-MM-DD, got '{ref}'")
        return cls(
            gcs_bucket=_env("GCS_BUCKET", required=True),
            gcp_project=_env("GCP_PROJECT_ID"),
            permalink_url_template=_env("PERMALINK_URL_TEMPLATE", DEFAULT_PERMALINK_URL_TEMPLATE),
            archive_url_template=_env("ARCHIVE_URL_TEMPLATE", DEFAULT_ARCHIVE_URL_TEMPLATE),
            http_timeout_s=int(_env("HTTP_TIMEOUT_SECONDS", "120")),
            timezone=_env("FETCHER_TIMEZONE", "Europe/Zurich"),
            reference_date=ref_date,
            tmp_dir=_env("FETCHER_TMP_DIR", "/tmp/fetcher"),
        )
