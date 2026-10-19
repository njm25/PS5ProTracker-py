"""Walks one target down its interval ramp.

The first challenge (or a run of plain failures) trips the target: by default it stops for the
rest of the run, so a block never repeats.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .alert import evaluate
from .challenge import detect
from .config import Options, Target
from .log import ProbeLog, SampleRecord, say
from .models import Challenge, FetchResult, ParseResult
from .parsers import Parser, safe_parse

Fetch = Callable[[], Awaitable[FetchResult]]


@dataclass
class TargetSummary:
    target: Target
    samples: int = 0
    parsed: int = 0
    changes: int = 0
    alerts: int = 0
    last_clean_interval: int | None = None
    tripped_at_interval: int | None = None
    trip_reason: str | None = None
    hold_clean: bool | None = None
    skipped: str | None = None
    latencies: list[int] = field(default_factory=list)

    def percentile(self, p: float) -> int | None:
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        return ordered[min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1)]


class _Stopped(Exception):
    pass


class Runner:
    def __init__(self, target: Target, options: Options, log: ProbeLog, fetch: Fetch, parse: Parser, once: bool = False):
        self.target = target
        self.summary = TargetSummary(target)
        # Called when this target trips, with the reason.
        self.on_trip: Callable[[Runner, str], None] | None = None
        self._options = options
        self._log = log
        self._fetch = fetch
        self._parse = parse
        self._once = once
        self._ramp = target.ramp
        self._stopped = asyncio.Event()
        self._stop_reason: str | None = None
        self._last_start: float | None = None
        self._last_fingerprint: str | None = None
        self._consecutive_errors = 0
        self._snapped_noparse = False

    def stop(self, reason: str | None = None) -> None:
        """Stops this target from outside: the run is ending, or a sibling on the same site tripped."""
        self._stop_reason = self._stop_reason or reason
        self._stopped.set()

    async def run(self) -> TargetSummary:
        intervals = [0] if self._once else self._ramp.intervals_seconds
        samples = 1 if self._once else self._ramp.samples_per_step
        try:
            for step, interval in enumerate(intervals, start=1):
                if await self._run_step(step, interval, samples):
                    self.summary.last_clean_interval = interval
                    continue

                if self._ramp.on_challenge == "cooldown_then_hold" and not self._once:
                    hold = self.summary.last_clean_interval
                    if hold is None:
                        hold = interval * 4
                    self._say(f"cooling down {self._ramp.cooldown_minutes} min, then one hold step at {hold}s")
                    await self._sleep(self._ramp.cooldown_minutes * 60)
                    self._consecutive_errors = 0
                    self._last_start = None
                    self.summary.hold_clean = await self._run_step(step + 1, hold, samples)
                break
        except _Stopped:
            if self._stop_reason:
                self.summary.trip_reason = self.summary.trip_reason or f"stopped: {self._stop_reason}"
                self._say(f"stopped: {self._stop_reason}")
            else:
                self._say("stopped (cancelled)")
        return self.summary

    async def _run_step(self, step: int, interval: int, samples: int) -> bool:
        """True if every sample was clean; False once one trips the target."""
        for s in range(1, samples + 1):
            await self._wait_for_slot(interval)
            trip = await self._sample(step, interval, s)
            if trip is not None:
                self.summary.tripped_at_interval = interval
                self.summary.trip_reason = trip
                self._say(f"TRIPPED at {interval}s: {trip}. Backing off.")
                if self.on_trip:
                    self.on_trip(self, trip)
                return False
        return True

    async def _wait_for_slot(self, interval: int) -> None:
        """Spaces sample starts by the interval (+/- jitter), however long each fetch took."""
        if self._last_start is not None and interval > 0:
            jitter = 1 + random.uniform(-1, 1) * self._ramp.jitter
            wait = self._last_start + interval * jitter - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
        self._last_start = time.monotonic()

    async def _sample(self, step: int, interval: int, sample: int) -> str | None:
        """Returns a trip reason, or None when the target may keep going."""
        t = self.target
        started = datetime.now(timezone.utc)
        f = await self._until_stopped(self._fetch())
        challenge = detect(f)
        parse = (
            safe_parse(self._parse, f.body, t)
            if challenge is Challenge.NONE
            else ParseResult.not_found(f"challenge: {challenge}")
        )
        alert = evaluate(parse, t, self._options.max_price)
        changed = (
            parse.signal_found and self._last_fingerprint is not None and parse.fingerprint != self._last_fingerprint
        )
        if parse.signal_found:
            self._last_fingerprint = parse.fingerprint

        self._log.write(SampleRecord(started, t, step, interval, sample, f, challenge, parse, alert, changed))
        s = self.summary
        s.samples += 1
        s.latencies.append(f.latency_ms)
        s.parsed += parse.signal_found
        s.changes += changed
        s.alerts += alert.eligible

        if s.samples == 1:
            self._log.snapshot(t.name, "first", f.body)
        if challenge is not Challenge.NONE:
            self._log.snapshot(t.name, f"challenge-{challenge}", f.body)
        elif changed:
            self._log.snapshot(t.name, "changed", f.body)
        if not parse.signal_found and challenge is Challenge.NONE and not self._snapped_noparse:
            self._snapped_noparse = True
            self._log.snapshot(t.name, "noparse", f.body)
        if alert.eligible:
            self._log.snapshot(t.name, "alert", f.body)

        status = "---" if f.status is None else f.status
        line = f"step {step} @{interval}s #{sample}: {status} {f.latency_ms}ms"
        if challenge is not Challenge.NONE:
            line += f" CHALLENGE={challenge}"
        if parse.signal_found:
            line += f" in_stock={parse.in_stock} [{parse.raw_signal}] seller={parse.seller} price={parse.price}"
        else:
            line += f" no-signal ({parse.note or f.error})"
        if changed:
            line += " CHANGED"
        self._say(line)
        if alert.eligible:
            self._say(f"*** WOULD ALERT: in stock at {parse.seller} for {parse.price} -> {t.product_url or t.url}")

        if challenge is not Challenge.NONE:
            return str(challenge)

        failed = not parse.signal_found or f.status is None or f.status >= 500
        self._consecutive_errors = self._consecutive_errors + 1 if failed else 0
        if self._consecutive_errors >= self._options.max_consecutive_errors:
            return f"{self._consecutive_errors} consecutive failures (last: {f.error or parse.note or f.status})"
        return None

    async def _sleep(self, seconds: float) -> None:
        await self._until_stopped(asyncio.sleep(seconds))

    async def _until_stopped(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Awaits coro, or raises _Stopped as soon as this target is stopped."""
        work = asyncio.ensure_future(coro)
        stop = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if self._stopped.is_set():
            work.cancel()
            raise _Stopped
        return work.result()

    def _say(self, message: str) -> None:
        say(self.target.name, message)
