"""Reads what the monitor and probe runs leave on disk: state, events, check history, runs, snapshots."""

from __future__ import annotations

import csv
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import config
from ..cli import _output_dir
from . import system

# The monitor saves state at least every check; older than this means it's stuck or down.
STALE_SECONDS = 180


def options() -> config.Options:
    return config.load()


def runs_dir() -> Path:
    return _output_dir(options().output_dir)


def monitor_dir() -> Path:
    return runs_dir() / ".monitor"


# --- monitor state ---

@dataclass
class TargetView:
    """One monitored target, ready for a card or a detail page."""

    raw: dict[str, Any]
    kind: str  # instock | other-stock | out | paused | cooldown | error | waiting
    label: str
    cooldown_until: float = 0
    stats: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.raw[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass
class MonitorView:
    service: str
    updated: float | None
    started: float | None
    targets: list[TargetView]
    retailers: dict[str, dict[str, Any]]
    webhook: bool

    @property
    def healthy(self) -> bool:
        return self.service == "active" and self.updated is not None and time.time() - self.updated < STALE_SECONDS

    @property
    def checks_24h(self) -> int:
        return sum(t.stats.get("checks", 0) for t in self.targets)

    @property
    def alerting(self) -> list[TargetView]:
        return [t for t in self.targets if t.kind == "instock"]

    def target(self, name: str) -> TargetView | None:
        return next((t for t in self.targets if t.name == name), None)


def _classify(t: dict[str, Any], cooldown_until: float, now: float) -> tuple[str, str]:
    if t.get("alerting"):
        return "instock", "In stock"
    if t.get("paused_until", 0) > now:
        return "paused", "Paused"
    if cooldown_until > now:
        return "cooldown", "Cooling down"
    if t.get("last_check") is None:
        return "waiting", "Waiting"
    if t.get("challenge") or not t.get("signal_found"):
        return "error", t.get("challenge") or "No signal"
    if t.get("in_stock"):
        return "other-stock", "In stock (no alert)"
    return "out", "Out of stock"


def monitor(with_stats: bool = True) -> MonitorView:
    try:
        state = json.loads((monitor_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    now = time.time()
    retailers = state.get("retailers", {})
    stats = check_stats() if with_stats else {}
    targets = []
    for t in state.get("targets", {}).values():
        until = retailers.get(t["retailer"], {}).get("cooldown_until", 0)
        kind, label = _classify(t, until, now)
        targets.append(TargetView(t, kind, label, until, stats.get(t["name"], {})))
    return MonitorView(
        service=system.unit_state("ps5probe-monitor"),
        updated=state.get("updated"),
        started=state.get("started"),
        targets=targets,
        retailers=retailers,
        webhook=bool(state.get("webhook")),
    )


# --- check history ---

def _check_files(hours: float) -> list[Path]:
    days = {(datetime.now() - timedelta(hours=h)).strftime("%Y%m%d") for h in range(0, int(hours) + 25, 24)}
    return sorted(p for d in days if (p := monitor_dir() / f"checks-{d}.csv").exists())


@lru_cache(maxsize=8)
def _read_checks(path: Path, mtime: float, size: int) -> list[tuple]:
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append((
                float(r["ts"]), r["target"], int(r["status"]) if r["status"] else None,
                int(r["latency_ms"]) if r["latency_ms"] else None,
                None if r["in_stock"] == "" else r["in_stock"] == "1", r["challenge"] or None, r["error"] or None,
            ))
    return rows


def checks(hours: float = 24, target: str | None = None) -> list[tuple]:
    """(ts, target, status, latency_ms, in_stock, challenge, error) rows, oldest first."""
    since = time.time() - hours * 3600
    out = []
    for p in _check_files(hours):
        st = p.stat()
        out += [r for r in _read_checks(p, st.st_mtime, st.st_size) if r[0] >= since and (target is None or r[1] == target)]
    return out


def check_stats(hours: float = 24) -> dict[str, dict[str, Any]]:
    """Per target: checks, ok %, median latency, and the last hour's latencies for a sparkline."""
    by_target: dict[str, list[tuple]] = {}
    for r in checks(hours):
        by_target.setdefault(r[1], []).append(r)
    hour_ago = time.time() - 3600
    out = {}
    for name, rows in by_target.items():
        ok = [r for r in rows if r[4] is not None]
        lat = [r[3] for r in ok if r[3] is not None]
        out[name] = {
            "checks": len(rows),
            "uptime": round(100 * len(ok) / len(rows)) if rows else None,
            "p50": round(statistics.median(lat)) if lat else None,
            "spark": [r[3] if r[4] is not None else None for r in rows if r[0] >= hour_ago][-60:],
        }
    return out


def sparkline(values: list[int | None], width: int = 120, height: int = 28) -> str:
    """SVG polyline points for latencies; failed checks drop to the bottom."""
    if len(values) < 2:
        return ""
    peak = max((v for v in values if v is not None), default=1) or 1
    step = width / (len(values) - 1)
    return " ".join(
        f"{i * step:.1f},{height - 2 - (height - 4) * ((v or 0) / peak):.1f}" for i, v in enumerate(values)
    )


# --- events ---

def events(limit: int = 50, kind: str | None = None, target: str | None = None,
           before: float | None = None) -> list[dict[str, Any]]:
    """Newest first."""
    path = monitor_dir() / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if (kind and e["kind"] != kind) or (target and e.get("target") != target) or (before and e["ts"] >= before):
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


EVENT_KINDS = ["alert", "gone", "change", "block", "resume", "command", "start", "stop", "reload", "heartbeat", "config"]


# --- snapshots and probe runs ---

def snapshots(target: str, limit: int = 20) -> list[dict[str, Any]]:
    folder = runs_dir() / "monitor" / "snapshots" / target
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.html"), reverse=True)[:limit]
    return [{"name": p.name, "reason": p.stem.rsplit("-", 1)[-1], "ts": p.stat().st_mtime, "size": p.stat().st_size}
            for p in files]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def safe_child(root: Path, *parts: str) -> Path | None:
    """root/parts, or None if that escapes root."""
    root = root.resolve()
    path = root.joinpath(*parts).resolve()
    return path if path.is_relative_to(root) and path != root else None


def runs() -> list[dict[str, Any]]:
    root = runs_dir()
    if not root.is_dir():
        return []
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")), reverse=True):
        summary = read_csv(d / "summary.csv")
        out.append({
            "name": d.name,
            "monitor": d.name == "monitor",
            "targets": len(summary) if summary else None,
            "samples": sum(int(r["samples"] or 0) for r in summary) if summary else None,
            "alerts": sum(int(r["would_alert"] or 0) for r in summary) if summary else None,
            "trips": sum(1 for r in summary if r["tripped_at_interval_s"]) if summary else None,
            "finished": bool(summary),
            "ts": d.stat().st_mtime,
        })
    return out
