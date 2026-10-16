"""NYISO zonal LBMP prices for the N.Y.C. zone (public CSVs, no key).

Real-time: 5-min intervals, http://mis.nyiso.com/public/csv/realtime/YYYYMMDDrealtime_zone.csv
Day-ahead: hourly,         http://mis.nyiso.com/public/csv/damlbmp/YYYYMMDDdamlbmp_zone.csv
Older days are only published as monthly zips (YYYYMM01<kind>_zone_csv.zip); we fall back to those.
Everything is cached under data/. Timestamps are NYISO local (Eastern prevailing time), returned as naive ISO strings.
Real-time stamps are interval-ending (first row 00:05); day-ahead stamps are hour-beginning.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

ZONE = "N.Y.C."
BASE = "http://mis.nyiso.com/public/csv"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "nyiso"
KINDS = {"rt": "realtime", "da": "damlbmp"}


def _as_date(d) -> date:
    if d is None:
        return date.today()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


def _fetch_csv(kind: str, d: date) -> str | None:
    """Return CSV text for one day, from cache, daily file, or monthly zip. None if unavailable."""
    folder = KINDS[kind]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{d:%Y%m%d}{folder}_zone.csv"
    path = DATA_DIR / name
    # Today's (and future) files are still being filled in; never trust a cached copy of those.
    # Day-ahead files are final once published; real-time files only after the day is over.
    final = True if kind == "da" else d < date.today()
    if path.exists() and final:
        return path.read_text()
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        r = c.get(f"{BASE}/{folder}/{name}")
        if r.status_code == 200 and "Time Stamp" in r.text[:200]:
            if final:
                path.write_text(r.text)
            return r.text
        zname = f"{d:%Y%m}01{folder}_zone_csv.zip"
        zpath = DATA_DIR / zname
        if not zpath.exists() or not final:
            zr = c.get(f"{BASE}/{folder}/{zname}")
            if zr.status_code != 200:
                return None
            zpath.write_bytes(zr.content)
    try:
        with zipfile.ZipFile(zpath) as z:
            if name not in z.namelist():
                return None
            text = z.read(name).decode()
    except zipfile.BadZipFile:
        zpath.unlink(missing_ok=True)
        return None
    path.write_text(text)
    return text


def _parse(text: str, zone: str = ZONE) -> list[dict]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("Name") != zone:
            continue
        raw = row["Time Stamp"]
        fmt = "%m/%d/%Y %H:%M:%S" if raw.count(":") == 2 else "%m/%d/%Y %H:%M"
        ts = datetime.strptime(raw, fmt)
        out.append({"ts": ts.isoformat(), "usd_mwh": float(row["LBMP ($/MWHr)"])})
    return out


def day_ahead(d=None) -> list[dict]:
    """Hourly day-ahead LBMP for N.Y.C. on date d: [{ts, usd_mwh}] (hour-beginning)."""
    text = _fetch_csv("da", _as_date(d))
    return _parse(text) if text else []


def real_time(d=None) -> list[dict]:
    """5-min real-time LBMP for N.Y.C. on date d: [{ts, usd_mwh}] (interval-ending)."""
    text = _fetch_csv("rt", _as_date(d))
    return _parse(text) if text else []


def night(d=None, kind: str = "da", start_hour: int = 19, end_hour: int = 5) -> list[dict]:
    """Prices covering the charging night that starts on date d (start_hour d .. end_hour d+1).

    For kind="da" tonight, tomorrow's day-ahead file is normally published by ~11am; if it is missing we reuse
    today's early-morning hours as a proxy (flagged with "proxy": True on those rows).
    """
    d = _as_date(d)
    fn = day_ahead if kind == "da" else real_time
    first, second = fn(d), fn(d + timedelta(days=1))
    t0 = datetime.combine(d, datetime.min.time()) + timedelta(hours=start_hour)
    t1 = datetime.combine(d + timedelta(days=1), datetime.min.time()) + timedelta(hours=end_hour)
    if kind == "da" and not second:
        second = [
            {"ts": (datetime.fromisoformat(p["ts"]) + timedelta(days=1)).isoformat(), "usd_mwh": p["usd_mwh"], "proxy": True}
            for p in first
        ]
    rows = []
    for p in first + second:
        ts = datetime.fromisoformat(p["ts"])
        if kind == "rt":
            ts -= timedelta(minutes=5)  # interval-ending -> interval-beginning
        if t0 <= ts < t1:
            rows.append({**p, "ts": ts.isoformat()})
    return rows


def history(days: int = 30, kind: str = "rt", end=None) -> dict[str, list[dict]]:
    """Night price series for the last `days` complete nights: {night_start_date_iso: [{ts, usd_mwh}]}.

    The most recent night is the one starting yesterday (it ends at 05:00 today, already settled).
    Nights with incomplete data are skipped.
    """
    end = _as_date(end) if end else date.today() - timedelta(days=1)
    out: dict[str, list[dict]] = {}
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        rows = night(d, kind)
        need = 120 if kind == "rt" else 10  # 10 hours of 5-min or hourly prices
        if len(rows) >= need * 0.9:
            out[d.isoformat()] = rows
    return out
