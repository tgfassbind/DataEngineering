"""All runtime configuration: environment variables + the versioned lines file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

RAW_KEY_TEMPLATE = "raw/{day}_istdaten.csv"
# Output of the transform, loaded into BigQuery by Kestra (see bigquery/sql/).
STOP_EVENTS_KEY_TEMPLATE = "processed/{day}_stop_events.parquet"
RUN_SCOPE_KEY_TEMPLATE = "processed/{day}_run_scope.parquet"
DEFAULT_LINES_FILE = Path(__file__).resolve().parent.parent / "config" / "lines.yml"

DEFAULT_MAX_ABS_DELAY_S = 10_800 # 3 h
DEFAULT_MAX_SEGMENT_DURATION_S = 21_600 # 6 h
DEFAULT_CHUNK_SIZE = 100_000 # CSV rows per chunk


class ConfigError(Exception):
    """Invalid or missing configuration -> CLI exit code 2."""


def raw_key(day: date) -> str:
    """Storage key of one day's raw file, e.g. raw/2026-09-21_istdaten.csv."""
    return RAW_KEY_TEMPLATE.format(day=day.isoformat())


def stop_events_key(day: date) -> str:
    """processed/2026-09-21_stop_events.parquet: validated rows of one day."""
    return STOP_EVENTS_KEY_TEMPLATE.format(day=day.isoformat())


def run_scope_key(day: date) -> str:
    """processed/2026-09-21_run_scope.parquet: the (day, line) pairs processed."""
    return RUN_SCOPE_KEY_TEMPLATE.format(day=day.isoformat())


@dataclass(frozen=True, order=True)
class Line:
    """One selected line. LINIEN_TEXT alone is not unique (several S-Bahn
    networks have an "S1"), so a line is always LINIEN_TEXT + BETREIBER_ABK."""
    linien_text: str
    betreiber_abk: str
    via_bpuic: tuple[int, ...] = ()

    @property
    def scope(self) -> tuple[str, str]:
        """The line as stored (dim_line natural key). A rerun replaces the
        day's rows of exactly these scopes in BigQuery."""
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
    # Data lake (Cloud Storage). Credentials are not configured here: the
    # Google client reads GOOGLE_APPLICATION_CREDENTIALS itself.
    gcs_bucket: str
    gcp_project: str | None  # empty -> taken from the service-account key
    # Processing
    chunk_size: int
    limits: Limits

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            gcs_bucket=_env("GCS_BUCKET", required=True),
            gcp_project=_env("GCP_PROJECT_ID"),
            chunk_size=_env_int("TRANSFORM_CHUNK_SIZE", DEFAULT_CHUNK_SIZE),
            limits=Limits(
                max_abs_delay_s=_env_int("TRANSFORM_MAX_ABS_DELAY_S", DEFAULT_MAX_ABS_DELAY_S),
                max_segment_duration_s=_env_int("TRANSFORM_MAX_SEGMENT_DURATION_S",
                                                DEFAULT_MAX_SEGMENT_DURATION_S),
            ),
        )
