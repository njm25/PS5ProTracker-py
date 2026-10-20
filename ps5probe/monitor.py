"""Phase 2: polls monitor.targets forever at fixed intervals and posts to Discord.

Posts a stock alert (with a ping) when a target starts meeting the alert rule, and quieter messages
when a stock signal changes, when it stops meeting the rule, and when a retailer blocks us. A block
pauses every target of that retailer; each further block in a row doubles the pause.

Everything lives under runs/.monitor/, for the web UI:
- state.json: the latest per target and retailer; reloaded on restart so a restart neither re-alerts
  nor skips a cooldown.
- events.jsonl: one line per alert, change, block, resume, start/stop, command; whether Discord took it.
- checks-YYYYMMDD.csv: one short row per check, kept CHECK_DAYS days.
- commands/: the UI drops {"cmd": ...} JSON files here; see _command().
Notable samples and their snapshots are appended to runs/monitor/. The process re-executes itself
when a source file or the config changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .alert import evaluate
from .challenge import detect
from .cli import _output_dir, build_fetch
from .config import Options, Target
from .fetchers import BrowserHost
from .log import ProbeLog, SampleRecord, say
from .models import Challenge, ParseResult
from .notify import AMBER, GREEN, GREY, RED, Notifier
from .parsers import PARSERS, safe_parse

RELOADED_ENV = "PS5_PROBE_MONITOR_RELOADED"
CHECK_DAYS = 14
PAUSED_FOREVER = 1e12  # "pause" with no minutes


def signal_key(p: ParseResult) -> str:
    """What has to differ between checks for a "signal changed" event.

    Only the first part of raw_signal (the stock field itself): later parts are cross-checks that
    flap on their own, like GameStop's button reading "Select Condition" on most loads.
    """
    primary = (p.raw_signal or "").split(";")[0].strip()
    return f"{p.in_stock}|{primary}|{p.seller}|{p.price}|{p.condition}"


@dataclass
class TargetState:
    name: str
    retailer: str
    interval: int
    url: str
    checks: int = 0
    blocks: int = 0
    last_check: float | None = None
    status: int | None = None
    latency_ms: int | None = None
    challenge: str | None = None
    error: str | None = None
    signal_found: bool | None = None
    in_stock: bool | None = None
    raw_signal: str | None = None
    seller: str | None = None
    price: str | None = None
    alert_reason: str | None = None
    consecutive_errors: int = 0
    next_check: float | None = None
    paused_until: float = 0
    # Kept across restarts (with the last signal above):
    signal_key: str | None = None
    alerting: bool = False
    last_change: float | None = None
    last_alert: float | None = None


@dataclass
class RetailerState:
    strikes: int = 0
    cooldown_until: float = 0
    last_trip: float = 0
    recovering: bool = False


class Monitor:
    def __init__(self, options: Options):
        self.options = options
        self.notifier = Notifier(options.discord)
        runs = _output_dir(options.output_dir)
        self.state_path = runs / ".monitor" / "state.json"
        self.log = ProbeLog(runs / "monitor", append=True)
        self.started = time.time()
        self.reload = False
        if options.monitor.headless is not None:
            options.headless = options.monitor.headless
        self._stop = asyncio.Event()
        self._closers: list[Callable[[], Awaitable[None]]] = []
        self._host = BrowserHost(options)
        self._closers.append(self._host.close)

        by_name = {t.name.lower(): t for t in options.targets}
        self.targets: list[Target] = [by_name[n.lower()] for n in options.monitor.targets]
        intervals = {n.lower(): s for n, s in options.monitor.targets.items()}
        self.states = {
            t.name: TargetState(t.name, t.retailer, intervals[t.name.lower()], t.product_url or t.url)
            for t in self.targets
        }
        self.retailers = {t.retailer: RetailerState() for t in self.targets}
        self._since_heartbeat = {t.name: [0, 0] for t in self.targets}  # checks, blocks
        self.dir = self.state_path.parent
        self._wake = {t.name: asyncio.Event() for t in self.targets}
        self._force: set[str] = set()  # targets to check now, even if paused
        self._load()

    # --- state file ---

    def _load(self) -> None:
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for name, s in saved.get("targets", {}).items():
            if (st := self.states.get(name)) is not None:
                # The last signal, so "changed" messages after a restart say what it changed from.
                for k in ("signal_key", "last_check", "last_change", "last_alert", "in_stock", "raw_signal", "seller",
                          "price", "paused_until", "signal_found", "status", "latency_ms", "challenge", "error",
                          "alert_reason"):
                    setattr(st, k, s.get(k, getattr(st, k)))
                st.alerting = bool(s.get("alerting"))
        for name, r in saved.get("retailers", {}).items():
            if name in self.retailers:
                self.retailers[name] = RetailerState(**{k: r[k] for k in asdict(RetailerState()) if k in r})

    def _save(self) -> None:
        state = {
            "pid": os.getpid(),
            "started": self.started,
            "updated": time.time(),
            "webhook": bool(self.options.discord.webhook_url),
            "targets": {n: asdict(s) for n, s in self.states.items()},
            "retailers": {n: asdict(r) for n, r in self.retailers.items()},
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # --- event log and check history ---

    def _event(self, kind: str, title: str, text: str = "", target: str | None = None,
               ping: bool = False, delivered: bool | None = None) -> None:
        st = self.states.get(target) if target else None
        line = {"ts": time.time(), "kind": kind, "target": target, "retailer": st.retailer if st else None,
                "title": title, "text": text, "ping": ping, "delivered": delivered}
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

    async def _notify(self, kind: str, text: str, *, target: str | None = None, **kwargs: Any) -> None:
        """Posts to Discord and records the event, with whether Discord took it (None: no webhook)."""
        ok = await self.notifier.send(text, **kwargs)
        self._event(kind, kwargs.get("title") or kind, text, target, kwargs.get("ping", False),
                    ok if self.options.discord.webhook_url else None)

    def _record_check(self, st: TargetState) -> None:
        day = datetime.now().strftime("%Y%m%d")
        path = self.dir / f"checks-{day}.csv"
        if not path.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text("ts,target,status,latency_ms,in_stock,challenge,error\n", encoding="utf-8")
            cutoff = time.time() - CHECK_DAYS * 86400
            for old in self.dir.glob("checks-*.csv"):
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
        error = (st.error or "").replace(",", ";").replace("\n", " ")[:120]
        in_stock = "" if st.in_stock is None or not st.signal_found else int(st.in_stock)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{st.last_check:.0f},{st.name},{st.status or ''},{st.latency_ms or ''},{in_stock},"
                     f"{st.challenge or ''},{error}\n")

    # --- main loop ---

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)

        quiet = os.environ.get(RELOADED_ENV) == "1"
        lines = [f"{t.name} every {self.states[t.name].interval}s" for t in self.targets]
        if quiet:
            say("monitor", "reloaded: " + ", ".join(lines))
            self._event("reload", "Monitor reloaded", "\n".join(lines))
        else:
            await self._notify("start", "\n".join(lines), title="Monitor started", color=GREY)

        tasks = [asyncio.create_task(self._poll(t)) for t in self.targets]
        tasks += [asyncio.create_task(self._heartbeat()), asyncio.create_task(self._watch())]
        self._save()
        await self._stop.wait()
        for task in tasks:
            task.cancel()
        # Loops also exit by themselves once _stop is set; never let one stuck task block a reload.
        await asyncio.wait(tasks, timeout=30)
        for close in self._closers:
            try:
                await close()
            except Exception as ex:  # e.g. the browser already died with the display
                say("monitor", f"close failed: {type(ex).__name__}: {ex}")
        self._save()
        self.log.close()
        if not self.reload:
            await self._notify("stop", "Stopped. No more stock checks until it starts again.",
                               title="Monitor stopped", color=GREY)

    async def _poll(self, t: Target) -> None:
        st = self.states[t.name]
        parse = PARSERS.get(t.parser)
        fetch, skip = build_fetch(t, self.options, self._host, self._closers)
        if parse is None or fetch is None:
            reason = skip or f"unknown parser '{t.parser}'"
            await self._notify("config", f"{t.name} skipped: {reason}", target=t.name,
                               title="Monitor config", color=AMBER)
            return
        # After a restart or reload, don't check sooner than the interval allows.
        if st.last_check and (wait := st.last_check + st.interval - time.time()) > 0:
            await self._nap(st, wait)
        while not self._stop.is_set():
            r = self.retailers[t.retailer]
            if (wait := r.cooldown_until - time.time()) > 0:
                await self._nap(st, wait)
                continue  # a sibling may have extended the cooldown meanwhile
            if (wait := st.paused_until - time.time()) > 0 and t.name not in self._force:
                await self._nap(st, wait)
                continue
            self._force.discard(t.name)
            started = time.time()
            try:
                await self._sample(t, st, fetch, parse)
                self._record_check(st)
            except Exception as ex:  # never let one bad sample kill the target's loop
                say(t.name, f"sample failed: {type(ex).__name__}: {ex}")
            jitter = 1 + random.uniform(-1, 1) * self.options.monitor.jitter
            await self._nap(st, max(1.0, st.interval * jitter - (time.time() - started)))

    async def _nap(self, st: TargetState, seconds: float) -> None:
        """Sleeps until the next check is due, or until a command wakes this target."""
        st.next_check = time.time() + seconds
        self._save()
        # asyncio.wait rather than wait_for: on 3.11, wait_for can swallow a cancel that lands just as
        # the event is set (a command and a reload in the same second), leaving the loop running.
        waiter = asyncio.ensure_future(self._wake[st.name].wait())
        try:
            await asyncio.wait({waiter}, timeout=seconds)
        finally:
            waiter.cancel()
        self._wake[st.name].clear()

    async def _sample(self, t: Target, st: TargetState, fetch, parse_fn) -> None:
        now = time.time()
        f = await fetch()
        challenge = detect(f)
        parse = safe_parse(parse_fn, f.body, t) if challenge is Challenge.NONE \
            else ParseResult.not_found(f"challenge: {challenge}")
        alert = evaluate(parse, t, self.options.max_price)

        st.checks += 1
        self._since_heartbeat[t.name][0] += 1
        st.last_check, st.status, st.latency_ms = now, f.status, f.latency_ms
        st.challenge = None if challenge is Challenge.NONE else str(challenge)
        st.error = f.error or (None if parse.signal_found else parse.note)
        st.signal_found = parse.signal_found

        changed = False
        if parse.signal_found:
            key = signal_key(parse)
            changed = st.signal_key is not None and key != st.signal_key
            old = (st.in_stock, st.raw_signal, st.seller, st.price)
            st.in_stock, st.raw_signal, st.seller = parse.in_stock, parse.raw_signal, parse.seller
            st.price, st.alert_reason = None if parse.price is None else str(parse.price), alert.reason
            await self._signal_events(t, st, parse, alert.eligible, changed, old)
            st.signal_key, st.alerting = key, alert.eligible

        notable = changed or alert.eligible or challenge is not Challenge.NONE or not parse.signal_found
        # "first": a target's very first check ever, not the first after each reload.
        first = st.checks == 1 and not (self.log.run_dir / "snapshots" / t.name).exists()
        if notable or first:
            self.log.write(SampleRecord(datetime.now(timezone.utc), t, 0, st.interval, st.checks,
                                        f, challenge, parse, alert, changed))
            reason = ("alert" if alert.eligible else f"challenge-{challenge}" if challenge is not Challenge.NONE
                      else "changed" if changed else "first" if first else "noparse")
            self.log.snapshot(t.name, reason, f.body)
            say(t.name, f"{f.status} {f.latency_ms}ms {st.challenge or ''} in_stock={parse.in_stock} "
                        f"[{parse.raw_signal}] {parse.seller} {parse.price} {alert.reason}")

        # A lone timeout is usually the network or a slow page, not a block: it only counts as a failure.
        if challenge not in (Challenge.NONE, Challenge.TIMEOUT):
            await self._trip(t, str(challenge))
            return
        failed = not parse.signal_found or f.status is None or f.status >= 500
        st.consecutive_errors = st.consecutive_errors + 1 if failed else 0
        if st.consecutive_errors >= self.options.max_consecutive_errors:
            await self._trip(t, f"{st.consecutive_errors} failures in a row (last: {st.error or f.status})")
        elif not failed:
            await self._clean(t)

    async def _signal_events(self, t: Target, st: TargetState, parse: ParseResult, eligible: bool,
                             changed: bool, old: tuple) -> None:
        link = t.product_url or t.url
        if eligible and not st.alerting:
            st.last_alert = time.time()
            await self._notify(
                "alert", f"In stock at {parse.seller} for ${parse.price}. Go buy it: {link}", target=t.name,
                ping=True, title=f"PS5 Pro IN STOCK: {t.retailer}", url=link, color=GREEN,
                fields={"Target": t.name, "Signal": parse.raw_signal, "Condition": parse.condition or "New"},
            )
        elif st.alerting and not eligible:
            await self._notify("gone", f"No longer meets the alert rule: {st.alert_reason}.", target=t.name,
                               title=f"{t.retailer}: stock gone", url=link, color=RED)
        elif changed and self.options.discord.notify_changes:
            was = f"in_stock={old[0]} [{old[1]}] {old[2]} ${old[3]}"
            now = f"in_stock={parse.in_stock} [{parse.raw_signal}] {parse.seller} ${parse.price}"
            await self._notify("change", f"Was: {was}\nNow: {now}\nAlert rule: {st.alert_reason}", target=t.name,
                               title=f"{t.retailer}: stock signal changed ({t.name})", url=link, color=AMBER)
        elif changed:
            self._event("change", f"{t.retailer}: stock signal changed ({t.name})", target=t.name)
        if changed:
            st.last_change = time.time()

    async def _trip(self, t: Target, reason: str) -> None:
        r = self.retailers[t.retailer]
        st = self.states[t.name]
        st.blocks += 1
        self._since_heartbeat[t.name][1] += 1
        if r.cooldown_until > time.time():
            return  # a sibling already paused this retailer
        m = self.options.monitor
        r.strikes += 1
        minutes = min(m.cooldown_minutes * 2 ** (r.strikes - 1), m.max_cooldown_minutes)
        r.cooldown_until, r.last_trip, r.recovering = time.time() + minutes * 60, time.time(), True
        for s in self.states.values():
            if s.retailer == t.retailer:
                s.consecutive_errors = 0
        say(t.name, f"BLOCKED: {reason}. Pausing {t.retailer} for {minutes} min.")
        text = f"{t.name}: {reason}. Pausing all {t.retailer} checks for {minutes} min."
        if self.options.discord.notify_blocks:
            await self._notify("block", text, target=t.name, title=f"{t.retailer}: blocked", color=RED)
        else:
            self._event("block", f"{t.retailer}: blocked", text, t.name)

    async def _clean(self, t: Target) -> None:
        r = self.retailers[t.retailer]
        if r.recovering:
            r.recovering = False
            text = f"{t.name} is getting clean responses again."
            if self.options.discord.notify_blocks:
                await self._notify("resume", text, target=t.name, title=f"{t.retailer}: resumed", color=GREY)
            else:
                self._event("resume", f"{t.retailer}: resumed", text, t.name)
        # A clean hour after the last block resets the backoff.
        if r.strikes and time.time() - max(r.last_trip, r.cooldown_until) > 3600:
            r.strikes = 0

    async def _heartbeat(self) -> None:
        hours = self.options.monitor.heartbeat_hours
        if hours <= 0:
            return
        while True:
            await asyncio.sleep(hours * 3600)
            lines = []
            for name, (checks, blocks) in self._since_heartbeat.items():
                st = self.states[name]
                lines.append(f"{name}: {checks} checks, {blocks} blocks, now in_stock={st.in_stock} [{st.raw_signal}]")
            self._since_heartbeat = {n: [0, 0] for n in self._since_heartbeat}
            await self._notify("heartbeat", "\n".join(lines), title=f"Still running (last {hours}h)", color=GREY)

    async def _watch(self) -> None:
        """Runs UI commands, and ends the run for a re-exec when the code or config changes."""
        def mtimes() -> dict[Path, float]:
            files = list(Path(__file__).parent.glob("*.py")) + [config.ROOT / "config.json", config.ROOT / "config.local.json"]
            return {p: p.stat().st_mtime for p in files if p.exists()}

        before = mtimes()
        inbox = self.dir / "commands"
        while True:
            await asyncio.sleep(1)
            for path in sorted(inbox.glob("*.json")) if inbox.is_dir() else []:
                try:
                    self._command(json.loads(path.read_text(encoding="utf-8")))
                except (ValueError, KeyError, TypeError) as ex:
                    say("monitor", f"bad command {path.name}: {ex}")
                path.unlink(missing_ok=True)
            if mtimes() != before:
                say("monitor", "code or config changed: reloading")
                self.reload = True
                self._stop.set()
                return

    def _command(self, c: dict[str, Any]) -> None:
        """check_now / pause [minutes] / resume take "target"; clear_cooldown takes "retailer"."""
        cmd, name = c["cmd"], c.get("target")
        st = self.states.get(name) if name else None
        if cmd == "clear_cooldown":
            r = self.retailers[c["retailer"]]
            r.cooldown_until, r.strikes = 0, 0
            names = [s.name for s in self.states.values() if s.retailer == c["retailer"]]
            title = f"Cooldown cleared: {c['retailer']}"
        elif st is None:
            raise KeyError(f"unknown target {name}")
        elif cmd == "check_now":
            self._force.add(name)
            names, title = [name], f"Check now: {name}"
        elif cmd == "pause":
            minutes = c.get("minutes")
            st.paused_until = time.time() + float(minutes) * 60 if minutes else PAUSED_FOREVER
            names, title = [name], f"Paused: {name}" + (f" for {minutes} min" if minutes else "")
        elif cmd == "resume":
            st.paused_until = 0
            names, title = [name], f"Resumed: {name}"
        else:
            raise KeyError(f"unknown command {cmd}")
        say("monitor", title)
        self._event("command", title, target=name)
        for n in names:
            self._wake[n].set()
        self._save()


def main(options: Options) -> int:
    if not options.monitor.targets:
        print("monitor.targets in config.json is empty.", file=sys.stderr)
        return 2
    monitor = Monitor(options)
    asyncio.run(monitor.run())
    if monitor.reload:
        os.execve(sys.executable, [sys.executable, "-u", "-m", "ps5probe", "--monitor"],
                  {**os.environ, RELOADED_ENV: "1"})
    return 0
