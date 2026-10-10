"""All runtime configuration: environment variables + the versioned lines file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

RAW_KEY_TEMPLATE = "raw/{day}_istdaten.csv"
DEFAULT_LINES_FILE = Path(__file__).resolve().parent.parent / "config" / "lines.yml"

DEFAULT_MAX_ABS_DELAY_S = 10_800 # 3 h
DEFAULT_MAX_SEGMENT_DURATION_S = 21_600 # 6 h
DEFAULT_CHUNK_SIZE = 100_000 # CSV rows per chunk


class ConfigError(Exception):
    """Invalid or missing configuration -> CLI exit code 2."""


def raw_key(day: date) -> str:
    """Storage key of one day's raw file, e.g. raw/2026-09-21_istdaten.csv."""
    return RAW_KEY_TEMPLATE.format(day=day.isoformat())


@dataclass(frozen=True, order=True)
class Line:
    """One selected line. LINIEN_TEXT alone is not unique (several S-Bahn
    networks have an "S1"), so a line is always LINIEN_TEXT + BETREIBER_ABK."""
    linien_text: str
    betreiber_abk: str
    via_bpuic: tuple[int, ...] = ()

    @property
    def scope(self) -> tuple[str, str]:
        """What staging rows are tagged with (and retransform deletes by). """
        return (self.linien_text, self.betreiber_abk)

    def __str__(self) -> str:
        via = "@" + "+".join(map(str, self.via_bpuic)) if self.via_bpuic else ""
        return f"{self.linien_text}:{self.betreiber_abk}{via}"


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    """unset or empty -> default; surrounding spaces stripped."""
    value = (os.environ.get(name) or "").strip() or default
    if required and not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got '{raw}'") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


# --------------------------------------------------------------------------- lines
def _parse_bpuic_list(values, where: str) -> tuple[int, ...]:
    out = set()
    for v in values:
        txt = str(v).strip()
        if not txt.isdigit():
            raise ConfigError(f"{where}: via_bpuic must be integer BPUIC stop ids, got {v!r}")
        out.add(int(txt))
    return tuple(sorted(out))

def parse_line_spec(spec: str) -> Line:
    """Parse one CLI entry "LINIEN_TEXT:BETREIBER_ABK[@BPUIC+BPUIC...]",
    e.g. "IR75:SBB" or "S1:SBB@8502202" (S1 via Rotkreuz)."""
    main, at, via = spec.strip().partition("@")
    text, sep, operator = spec.strip().rpartition(":")
    if not sep or not text.strip() or not operator.strip() or (at and not via.strip()):
        raise ConfigError(
            f"Invalid line '{spec}': expected LINIEN_TEXT:BETREIBER_ABK[@BPUIC+BPUIC], "
            f"e.g. IR75:SBB or S1:SBB@8502202")
    stops = _parse_bpuic_list(via.split("+"), f"--lines {spec}") if at else ()
    return Line(text.strip(), operator.strip(), stops)


def load_lines_file(path: Path) -> list[Line]:
    """Read the versioned YAML file:

        lines:
          - linien_text: IR75
            betreiber_abk: SBB
            via_bpuic: [8502202]
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"Lines file not found: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"Lines file {path} is not valid YAML: {e}") from None
    entries = doc.get("lines") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"Lines file {path} must contain a non-empty list 'lines'")
    lines = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: entry {i} must be a mapping")
        text = str(entry.get("linien_text") or "").strip()
        operator = str(entry.get("betreiber_abk") or "").strip()
        if not text or not operator:
            raise ConfigError(f"{path}: entry {i} needs both linien_text and betreiber_abk")
        via = entry.get("via_bpuic") or []
        if not isinstance(via, list):
            raise ConfigError(f"{path}: entry {i}: via_bpuic must be a list")
        lines.append(Line(text, operator, _parse_bpuic_list(via, f"{path}: entry {i}")))
    return _dedupe(lines)


def _dedupe(lines: list[Line]) -> list[Line]:
    return sorted(set(lines))


def resolve_lines(cli_lines: list[str] | None) -> tuple[list[Line], str]:
    """Precedence: CLI --lines > TRANSFORM_LINES_FILE (path) > shipped config/lines.yml.

    Returns the lines and a description of where they came from (for the log).
    """
    if cli_lines is not None:
        lines = _dedupe([parse_line_spec(s) for item in cli_lines
                         for s in item.split(",") if s.strip()])
        if not lines:
            raise ConfigError("--lines was given but contains no line")
        return lines, "--lines"
    env_path = _env("TRANSFORM_LINES_FILE")
    if env_path:
        return load_lines_file(Path(env_path)), env_path
    return load_lines_file(DEFAULT_LINES_FILE), str(DEFAULT_LINES_FILE)

@dataclass(frozen=True)
class Limits:
    """Validation limits (see README: validation rules)."""
    max_abs_delay_s: int = DEFAULT_MAX_ABS_DELAY_S
    max_segment_duration_s: int = DEFAULT_MAX_SEGMENT_DURATION_S


@dataclass(frozen=True)
class Config:
    """All settings of one transform run, read once at startup."""
    s3_endpoint_url: str | None # empty -> real AWS S3
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str
    s3_bucket: str
    s3_bucket_wait_s: int # how long to wait for the bucket to appear
    # PostgreSQL
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str
    # Processing
    chunk_size: int
    limits: Limits

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            s3_endpoint_url=_env("S3_ENDPOINT_URL"),
            s3_access_key_id=_env("S3_ACCESS_KEY_ID", required=True),
            s3_secret_access_key=_env("S3_SECRET_ACCESS_KEY", required=True),
            s3_region=_env("S3_REGION", "us-east-1"),
            s3_bucket=_env("S3_BUCKET", required=True),
            s3_bucket_wait_s=_env_int("S3_BUCKET_WAIT_SECONDS", 60, minimum=0),
            pg_host=_env("POSTGRES_HOST", "database"),
            pg_port=_env_int("POSTGRES_PORT", 5432),
            pg_db=_env("POSTGRES_DB", "travelbuddy"),
            pg_user=_env("POSTGRES_USER", required=True),
            pg_password=_env("POSTGRES_PASSWORD", required=True),
            chunk_size=_env_int("TRANSFORM_CHUNK_SIZE", DEFAULT_CHUNK_SIZE),
            limits=Limits(
                max_abs_delay_s=_env_int("TRANSFORM_MAX_ABS_DELAY_S", DEFAULT_MAX_ABS_DELAY_S),
                max_segment_duration_s=_env_int("TRANSFORM_MAX_SEGMENT_DURATION_S",
                                                DEFAULT_MAX_SEGMENT_DURATION_S),
            ),
        )

    def pg_conninfo(self) -> dict:
        """Keyword arguments for psycopg.connect (password never logged)."""
        return dict(host=self.pg_host, port=self.pg_port, dbname=self.pg_db,
                    user=self.pg_user, password=self.pg_password,
                    application_name="deng-transform")
