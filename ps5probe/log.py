"""samples.csv (one row per request), page snapshots, and console lines."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Target
from .models import AlertDecision, Challenge, FetchResult, ParseResult

COLUMNS = [
    "timestamp_utc", "target", "retailer", "method", "step", "interval_s", "sample",
    "http_status", "latency_ms", "bytes", "challenge", "signal_parsed", "in_stock", "raw_signal",
    "seller", "price", "condition", "alert_eligible", "alert_reason", "changed",
    "cache_headers", "final_url", "error", "note",
]


@dataclass(frozen=True)
class SampleRecord:
    timestamp: datetime
    target: Target
    step: int
    interval_seconds: int
    sample: int
    fetch: FetchResult
    challenge: Challenge
    parse: ParseResult
    alert: AlertDecision
    changed: bool


def csv_bool(v: bool | None) -> str:
    return "" if v is None else "true" if v else "false"


def say(target: str, message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {target:<16} {message}", flush=True)


def _millis(dt: datetime) -> str:
    return f"{dt:%Y%m%d-%H%M%S}-{dt.microsecond // 1000:03d}"


class ProbeLog:
    """Appends one CSV row per sample and keeps body snapshots for anything worth re-reading."""

    def __init__(self, run_dir: Path, append: bool = False):
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "samples.csv"
        new = not append or not path.exists() or path.stat().st_size == 0
        self._file = open(path, "w" if new else "a", newline="", encoding="utf-8")
        self._csv = csv.writer(self._file, lineterminator="\n")
        if new:
            self._csv.writerow(COLUMNS)

    def write(self, r: SampleRecord) -> None:
        f, p = r.fetch, r.parse
        ts = r.timestamp.astimezone(timezone.utc)
        self._csv.writerow([
            f"{ts:%Y-%m-%dT%H:%M:%S}.{ts.microsecond // 1000:03d}Z",
            r.target.name, r.target.retailer, r.target.method, r.step, r.interval_seconds, r.sample,
            f.status, f.latency_ms, len(f.body.encode()),
            r.challenge, csv_bool(p.signal_found), csv_bool(p.in_stock), p.raw_signal,
            p.seller, p.price, p.condition, csv_bool(r.alert.eligible), r.alert.reason, csv_bool(r.changed),
            "; ".join(f"{k}={v}" for k, v in f.headers.items()), f.final_url, f.error, p.note,
        ])
        self._file.flush()

    def snapshot(self, target: str, reason: str, body: str) -> Path:
        folder = self.run_dir / "snapshots" / target
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_millis(datetime.now(timezone.utc))}-{reason}.html"
        path.write_text(body, encoding="utf-8")
        return path

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> ProbeLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
