"""Command line: pick targets, run them side by side, write samples.csv and summary.csv."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .config import Options, Target
from .fetchers import BrowserFetcher, BrowserHost, HttpFetcher, WalmartSigner
from .log import ProbeLog, csv_bool, say
from .parsers import PARSERS
from .runner import Fetch, Runner, TargetSummary


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.install_browsers:
        return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])

    try:
        options = config.load()
        profile = config.apply_profile(options, args.profile) if args.profile else None
    except config.ConfigError as ex:
        print(ex, file=sys.stderr)
        return 2
    if args.headed:
        options.headless = False

    if args.list:
        _list(options)
        return 0

    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    selected = only if only is not None else list(profile.get("targets") or {}) if profile else None
    wanted = {s.lower() for s in selected} if selected is not None else None
    targets = [t for t in options.targets if (t.enabled if wanted is None else t.name.lower() in wanted)]
    unknown = [n for n in only or [] if n.lower() not in {t.name.lower() for t in targets}]
    if unknown:
        print(f"Unknown target(s): {', '.join(unknown)}. Use --list.", file=sys.stderr)
        return 2

    suffix = (f"-{args.profile}" if args.profile else "") + ("-once" if args.once else "")
    run_dir = _output_dir(options.output_dir) / (datetime.now().strftime("%Y%m%d-%H%M%S") + suffix)
    try:
        asyncio.run(_run(options, targets, run_dir, args.once))
    except KeyboardInterrupt:  # a second Ctrl+C while shutting down
        return 130
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ps5probe", description="Phase 1 PS5 Pro stock probe.")
    p.add_argument("--install-browsers", action="store_true", help="install Playwright Chromium (once)")
    p.add_argument("--list", action="store_true", help="show targets and profiles")
    p.add_argument("--once", action="store_true", help="one sample per target")
    p.add_argument("--only", metavar="NAMES", help="comma-separated target names")
    p.add_argument("--profile", metavar="NAME", help="named run from config.json profiles")
    p.add_argument("--headed", action="store_true", help="visible Chromium for browser targets")
    return p.parse_args(argv)


def _list(options: Options) -> None:
    for t in options.targets:
        intervals = ",".join(map(str, t.ramp.intervals_seconds))
        print(f"{t.name:<18} {'on ' if t.enabled else 'off'} {t.method:<22} {t.parser:<22} {intervals}s  {t.url}")
    for name, p in options.profiles.items():
        headed = " (headed)" if p.get("headless") is False else ""
        print(f"profile {name}: {', '.join(p.get('targets') or {})}{headed}")


async def _run(options: Options, targets: list[Target], run_dir: Path, once: bool) -> None:
    with ProbeLog(run_dir) as log:
        (run_dir / "targets.json").write_text(json.dumps([asdict(t) for t in targets], indent=2), encoding="utf-8")
        print(f"Run dir: {run_dir}")
        print(
            f"Targets: {', '.join(t.name for t in targets)}"
            + (" (one sample each)" if once else "")
            + ("" if options.headless else " [headed]")
        )

        host = BrowserHost(options)
        closers: list[Callable[[], Awaitable[None]]] = [host.close]
        summaries: list[TargetSummary] = []
        runners: list[Runner] = []

        def stop_siblings(tripped: Runner, reason: str) -> None:
            # Targets of one retailer share this IP's reputation there: one trip stops them all.
            for r in runners:
                if r is not tripped and r.target.retailer == tripped.target.retailer:
                    r.stop(f"{tripped.target.name} tripped ({reason})")

        for t in targets:
            parse = PARSERS.get(t.parser)
            if parse is None:
                summaries.append(TargetSummary(t, skipped=f"unknown parser '{t.parser}'"))
                continue
            fetch, skip = _build_fetch(t, options, host, closers)
            if fetch is None:
                say(t.name, f"skipped: {skip}")
                summaries.append(TargetSummary(t, skipped=skip))
                continue
            runner = Runner(t, options, log, fetch, parse, once)
            runner.on_trip = stop_siblings
            runners.append(runner)

        tasks = [asyncio.create_task(r.run()) for r in runners]
        max_seconds = None if once or options.max_run_minutes is None else options.max_run_minutes * 60
        try:
            if tasks:
                await asyncio.wait(tasks, timeout=max_seconds)
        except asyncio.CancelledError:  # Ctrl+C: stop cleanly and still write the summary
            asyncio.current_task().uncancel()
        for r in runners:
            r.stop()
        summaries += await asyncio.gather(*tasks)
        for close in closers:
            await close()

        _write_summary(run_dir, summaries)


def _build_fetch(
    t: Target, options: Options, host: BrowserHost, closers: list[Callable[[], Awaitable[None]]]
) -> tuple[Fetch | None, str | None]:
    """The fetch function for a target, or None and the reason it's skipped."""
    match t.method:
        case "http":
            http = HttpFetcher(options)
            closers.append(http.close)
            # {nonce} makes every URL unique, to get past a shared cache.
            return lambda: http.fetch(t.url.replace("{nonce}", str(int(time.time() * 1000))), t.headers), None

        case "browser":
            browser = BrowserFetcher(host, options)
            closers.append(browser.close)
            return lambda: browser.fetch(t), None

        case "bestbuy_api":
            key = options.credentials.bestbuy_api_key
            if not key:
                return None, "no credentials.bestbuy_api_key"
            http = HttpFetcher(options)
            closers.append(http.close)

            async def fetch_bestbuy():
                r = await http.fetch(t.url.replace("{bestbuy_api_key}", key))
                return replace(
                    r,
                    final_url=r.final_url.replace(key, "REDACTED"),
                    error=r.error.replace(key, "REDACTED") if r.error else None,
                )

            return fetch_bestbuy, None

        case "walmart_affiliate_api":
            c = options.credentials
            if not c.walmart_consumer_id or not c.walmart_private_key_path:
                return None, "no credentials.walmart_consumer_id / walmart_private_key_path"
            signer = WalmartSigner(
                c.walmart_consumer_id, c.walmart_key_version, Path(c.walmart_private_key_path).read_bytes()
            )
            http = HttpFetcher(options)
            closers.append(http.close)
            return lambda: http.fetch(t.url, signer.headers()), None

        case _:
            return None, f"unsupported method {t.method}"


def _write_summary(run_dir: Path, summaries: list[TargetSummary]) -> None:
    def blank(v: Any) -> Any:
        return "" if v is None else v

    def secs(v: int | None) -> str:
        return "-" if v is None else f"{v}s"

    print()
    print(f"{'target':<16} {'samples':>7} {'parsed':>6} {'clean@':>7} {'trip@':>6} {'p50ms':>6} {'p95ms':>6}  reason")
    with open(run_dir / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow([
            "target", "method", "samples", "parsed", "changes", "would_alert", "last_clean_interval_s",
            "tripped_at_interval_s", "trip_reason", "hold_clean", "latency_p50_ms", "latency_p95_ms", "skipped",
        ])
        for s in sorted(summaries, key=lambda s: s.target.name):
            p50, p95 = s.percentile(0.5), s.percentile(0.95)
            w.writerow([
                s.target.name, s.target.method, s.samples, s.parsed, s.changes, s.alerts, s.last_clean_interval,
                s.tripped_at_interval, s.trip_reason, csv_bool(s.hold_clean), p50, p95, s.skipped,
            ])
            print(
                f"{s.target.name:<16} {s.samples:>7} {s.parsed:>6} {secs(s.last_clean_interval):>7} "
                f"{secs(s.tripped_at_interval):>6} {blank(p50):>6} {blank(p95):>6}  {s.skipped or s.trip_reason or ''}"
            )
    print(f"\nWrote {run_dir / 'samples.csv'} and summary.csv")


def _output_dir(dir_: str) -> Path:
    """Relative output dirs land in the checkout, wherever you run from."""
    p = Path(dir_)
    return p if p.is_absolute() else config.ROOT / p
