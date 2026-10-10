"""Shared test helpers: fixture path and synthetic Ist-Daten rows."""
from __future__ import annotations

import csv
import io
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "dataset" / "excerpt_21092026_istdaten_v2.csv"
FIXTURE_DAY = "2026-09-21"

HEADER = [
    "BETRIEBSTAG", "FAHRT_BEZEICHNER", "BETREIBER_ID", "BETREIBER_ABK", "BETREIBER_NAME",
    "PRODUKT_ID", "LINIEN_ID", "LINIEN_TEXT", "UMLAUF_ID", "VERKEHRSMITTEL_TEXT",
    "ZUSATZFAHRT_TF", "FAELLT_AUS_TF", "BPUIC", "HALTESTELLEN_NAME", "ANKUNFTSZEIT",
    "AN_PROGNOSE", "AN_PROGNOSE_STATUS", "ABFAHRTSZEIT", "AB_PROGNOSE",
    "AB_PROGNOSE_STATUS", "DURCHFAHRT_TF", "SLOID",
]


def fixture_rows() -> list[dict]:
    """The fixture parsed with the csv module only (independent of app code)."""
    with open(FIXTURE, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def make_row(trip: str, bpuic: int, name: str, arr: str = "", arr_act: str = "",
             arr_status: str = "", dep: str = "", dep_act: str = "", dep_status: str = "",
             line: str = "IR75", operator: str = "SBB", day: str = "21.09.2026",
             cancelled: str = "false", through: str = "false", extra: str = "false") -> dict:
    """One raw row; times are given as 'HH:MM' / 'HH:MM:SS' on `day`, or as a
    full 'DD.MM.YYYY HH:MM[:SS]' string."""
    def ts(v: str) -> str:
        return v if (not v or "." in v) else f"{day} {v}"
    return {
        "BETRIEBSTAG": day, "FAHRT_BEZEICHNER": trip, "BETREIBER_ID": "85:11",
        "BETREIBER_ABK": operator, "BETREIBER_NAME": operator, "PRODUKT_ID": "Zug",
        "LINIEN_ID": "1", "LINIEN_TEXT": line, "UMLAUF_ID": "", "VERKEHRSMITTEL_TEXT": "IR",
        "ZUSATZFAHRT_TF": extra, "FAELLT_AUS_TF": cancelled, "BPUIC": str(bpuic),
        "HALTESTELLEN_NAME": name, "ANKUNFTSZEIT": ts(arr), "AN_PROGNOSE": ts(arr_act),
        "AN_PROGNOSE_STATUS": arr_status, "ABFAHRTSZEIT": ts(dep), "AB_PROGNOSE": ts(dep_act),
        "AB_PROGNOSE_STATUS": dep_status, "DURCHFAHRT_TF": through, "SLOID": "",
    }


def to_csv_bytes(rows: list[dict], sep: str = ",") -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, delimiter=sep, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")
