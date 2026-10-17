"""Pure-Python reference of reporting.od_delay_by_slot (independent of the SQL).

Input: staging rows as tuples in helpers.STAGING_COLS order.
Output: report rows in helpers.REPORT_COLS order, medians as text (like
helpers.REPORT_SELECT), sorted like REPORT_SELECT.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, localcontext


def median_minutes(seconds: list[int]) -> str | None:
    """Median of signed seconds -> minutes, rounded once to 0.1, half away from zero."""
    if not seconds:
        return None
    v = sorted(seconds)
    n = len(v)
    with localcontext() as ctx:
        ctx.prec = 50
        med = Decimal(v[n // 2]) if n % 2 else (Decimal(v[n // 2 - 1]) + Decimal(v[n // 2])) / 2
        # ROUND_HALF_UP in decimal = half away from zero (-0.05 -> -0.1)
        q = (med / Decimal(60)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if q == 0:
        q = Decimal("0.0")       # no negative zero
    return str(q)


def _min_name(names):
    present = [n for n in names if n is not None]
    return min(present) if present else None  # code point order = UTF-8 byte order = COLLATE "C"


def reference(staging_rows: list[tuple]) -> list[tuple]:
    trips = defaultdict(list)
    for r in staging_rows:
        (day, trip, bpuic, name, line, op, seq, s_arr, s_dep, arr, dep, _extra, _hash) = r
        trips[(day, trip)].append(dict(bpuic=bpuic, name=name, line=line, op=op, seq=seq,
                                       s_dep=s_dep, arr=arr, dep=dep))
    groups = defaultdict(lambda: {"from": [], "to": [], "dep": [], "arr": []})
    for stops in trips.values():
        for a in stops:
            if a["s_dep"] is None:
                continue
            for b in stops:
                if b["seq"] <= a["seq"]:
                    continue
                key = (a["line"], a["op"], a["bpuic"], b["bpuic"],
                       a["s_dep"].isoweekday(), a["s_dep"].hour)
                g = groups[key]
                g["from"].append(a["name"])
                g["to"].append(b["name"])
                if a["dep"] is not None:
                    g["dep"].append(a["dep"])
                if b["arr"] is not None:
                    g["arr"].append(b["arr"])
    out = []
    for (line, op, f, t, wd, hr), g in groups.items():
        if not g["dep"] and not g["arr"]:
            continue
        out.append((line, op, f, _min_name(g["from"]), t, _min_name(g["to"]), wd, hr,
                    median_minutes(g["dep"]), median_minutes(g["arr"]), len(g["dep"]), len(g["arr"])))
    # same order as REPORT_SELECT (COLLATE "C" on text = code point order)
    out.sort(key=lambda r: (r[0], r[1], r[2], r[4], r[6], r[7]))
    return out