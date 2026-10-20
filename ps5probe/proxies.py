"""A pool of HTTP proxies (e.g. Webshare static residential): the list, its cache, and which proxy to use next.

Blocks are per proxy and retailer: a proxy Walmart blocked rests for that retailer only, and the next
check uses another. Proxy passwords never leave this module except inside the URLs handed to clients.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

FAILS_TO_REST = 3  # timeouts/errors in a row before a proxy rests for a retailer


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"http://{auth}{self.host}:{self.port}"

    def playwright(self) -> dict[str, str]:
        d = {"server": f"http://{self.host}:{self.port}"}
        if self.username:
            d.update(username=self.username, password=self.password or "")
        return d

    def redact(self, text: str) -> str:
        return text.replace(self.password, "***") if self.password else text


def parse_list(text: str) -> list[Proxy]:
    """One proxy per line: ip:port, ip:port:user:pass, or http://user:pass@ip:port. Blank lines and # comments skipped."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            u = httpx.URL(line)
            out.append(Proxy(u.host, u.port or 80, u.username or None, u.password or None))
            continue
        parts = line.split(":")
        if len(parts) not in (2, 4) or not parts[1].isdigit():
            raise ValueError(f"can't read proxy line {len(out) + 1}: expected ip:port or ip:port:user:pass")
        out.append(Proxy(parts[0], int(parts[1]), *(parts[2:] or [None, None])))
    return out


def download(url: str) -> list[Proxy]:
    r = httpx.get(url, timeout=20, follow_redirects=True)
    r.raise_for_status()
    proxies = parse_list(r.text)
    if not proxies:
        raise ValueError("the list is empty")
    return proxies


def _source(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def save_cache(path: Path, url: str, proxies: list[Proxy]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched": time.time(), "source": _source(url),
                               "proxies": [asdict(p) for p in proxies]}), encoding="utf-8")
    os.chmod(tmp, 0o600)  # it holds the proxy passwords
    os.replace(tmp, path)


def read_cache(path: Path, url: str) -> tuple[list[Proxy], float] | None:
    """The cached list and when it was fetched, if it came from this URL."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("source") != _source(url):
        return None
    return [Proxy(**p) for p in d["proxies"]], d["fetched"]


@dataclass
class ProxyStats:
    requests: int = 0
    failures: int = 0
    blocks: int = 0
    day: str = ""
    bytes_day: int = 0
    last_used: float = 0
    last_error: str | None = None


@dataclass
class Rest:
    """A proxy's standing with one retailer."""
    until: float = 0
    strikes: int = 0
    fails: int = 0


@dataclass
class Pool:
    proxies: list[Proxy]
    cooldown_minutes: int = 30
    max_cooldown_minutes: int = 240
    stats: dict[str, ProxyStats] = field(default_factory=dict)
    rests: dict[str, dict[str, Rest]] = field(default_factory=dict)  # retailer -> proxy label -> Rest
    _turn: dict[str, int] = field(default_factory=dict)
    _sticky: dict[str, str] = field(default_factory=dict)  # target -> proxy label

    def _rest(self, retailer: str, p: Proxy) -> Rest:
        return self.rests.setdefault(retailer, {}).setdefault(p.label, Rest())

    def available(self, retailer: str) -> list[Proxy]:
        now = time.time()
        return [p for p in self.proxies if self._rest(retailer, p).until <= now]

    def next_free(self, retailer: str) -> float:
        """When the first resting proxy is free again for this retailer."""
        return min((self._rest(retailer, p).until for p in self.proxies), default=0)

    def pick(self, retailer: str, target: str, sticky: bool) -> Proxy | None:
        free = self.available(retailer)
        if not free:
            return None
        if sticky:
            current = next((p for p in free if p.label == self._sticky.get(target)), None)
            if current is None:
                # The least recently used free proxy, so a target doesn't land where another just was.
                current = min(free, key=lambda p: self.stats.get(p.label, ProxyStats()).last_used)
                self._sticky[target] = current.label
            return current
        turn = self._turn.get(target, -1) + 1
        self._turn[target] = turn
        return free[turn % len(free)]

    def record(self, retailer: str, p: Proxy, nbytes: int, ok: bool, error: str | None = None) -> bool:
        """Counts a check through p; returns True if p now rests for this retailer after repeated failures."""
        s = self.stats.setdefault(p.label, ProxyStats())
        today = date.today().isoformat()
        if s.day != today:
            s.day, s.bytes_day = today, 0
        s.requests += 1
        s.bytes_day += nbytes
        s.last_used = time.time()
        rest = self._rest(retailer, p)
        if ok:
            rest.fails = 0
            if rest.strikes and time.time() - rest.until > 3600:
                rest.strikes = 0  # a clean hour after the last rest resets the backoff
            return False
        s.failures += 1
        s.last_error = p.redact(error or "failed")[:120]
        rest.fails += 1
        if rest.fails >= FAILS_TO_REST:
            self.block(retailer, p, s.last_error, count=False)
            return True
        return False

    def block(self, retailer: str, p: Proxy, reason: str, count: bool = True) -> int:
        """Rests p for this retailer; returns the minutes."""
        rest = self._rest(retailer, p)
        rest.strikes += 1
        rest.fails = 0
        minutes = min(self.cooldown_minutes * 2 ** (rest.strikes - 1), self.max_cooldown_minutes)
        rest.until = time.time() + minutes * 60
        s = self.stats.setdefault(p.label, ProxyStats())
        if count:
            s.blocks += 1
            s.last_error = p.redact(reason)[:120]
        return minutes

    def mb_today(self) -> float:
        today = date.today().isoformat()
        return sum(s.bytes_day for s in self.stats.values() if s.day == today) / 1e6

    def clear(self, retailer: str) -> None:
        self.rests.pop(retailer, None)

    # --- state.json ---

    def dump(self) -> dict[str, Any]:
        return {
            "count": len(self.proxies),
            "stats": {k: asdict(v) for k, v in self.stats.items()},
            "rests": {r: {k: asdict(v) for k, v in by.items() if v.until or v.strikes or v.fails}
                      for r, by in self.rests.items()},
        }

    def restore(self, d: dict[str, Any]) -> None:
        labels = {p.label for p in self.proxies}
        self.stats = {k: ProxyStats(**v) for k, v in (d.get("stats") or {}).items() if k in labels}
        self.rests = {r: {k: Rest(**v) for k, v in by.items() if k in labels}
                      for r, by in (d.get("rests") or {}).items()}
