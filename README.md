# PS5 Pro Tracker

Detects when a PS5 Pro is in stock at retail price, sold by the retailer itself, and New. Buying is manual. Goals and scope live in the original goals doc; findings are in [docs/](docs/).

This is **Phase 1**: a measurement harness, the `ps5probe` package. It polls each retailer on a slowly tightening interval and logs what happens: status, latency, blocks, and whether the stock signal parsed. Phase 2 pollers then get intervals from real data.

Python port of the original .NET `PS5Tracker.Probe`; same targets, parsers, ramp and output files.

## Setup

Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; source .venv/bin/activate elsewhere
pip install -e ".[dev]"
python -m ps5probe --install-browsers   # once: Playwright Chromium
```

## Run it

```bash
python -m ps5probe --list               # show targets
python -m ps5probe --once               # one sample per enabled target
python -m ps5probe --only psdirect-api,samsclub-page
python -m ps5probe --headed             # visible Chromium for browser targets
python -m ps5probe                      # full ramp, all enabled targets
python -m ps5probe --profile fast       # push below the measured clean floors
python -m ps5probe --profile headed     # Best Buy / GameStop / Newegg in visible Chromium
```

`ps5probe` works too once the package is installed. Ctrl+C stops cleanly and still writes the summary. `max_run_minutes` (default 180) caps a full run.

### Profiles

`profiles` in `config.json` picks targets by name and overrides their `ramp`, `settle_ms`, `headless` or `max_run_minutes`. Disabled targets still run if a profile names them. `--only` takes precedence over a profile's target list.

- **fast**: starts each target at the fastest interval that was clean on 2026-10-15 and goes lower. It also runs `psdirect-api-bust` next to `psdirect-api`. The bust variant has a unique `_={nonce}` per request and sends `Cache-Control: no-cache`, so the two can be compared for CDN staleness.
- **headed**: Best Buy, GameStop and Newegg in a visible Chromium window, from 300s down to 30s. Headless was rejected by all three.

When any target trips, other targets with the same `retailer` stop as well, since they share this IP's standing with that site.

Output goes to `runs/<timestamp>[-profile]/` in the checkout, wherever you run from (git-ignored):
- `samples.csv`: one row per request. Columns: timestamp, method, interval, HTTP status, latency, challenge, signal parsed, in stock, raw signal, seller, price, condition, would-alert and why, changed-since-last, cache headers, error.
- `summary.csv`: per target, the last clean interval, the interval and reason it tripped at, and p50/p95 latency.
- `snapshots/<target>/`: page bodies for the first sample, any challenge, any signal change, the first unparsable page, and anything that would alert.

## How the ramp and backoff work

Each target walks its `intervals_seconds` from slowest to fastest, taking `samples_per_step` samples at each step (±15% jitter). A target **trips** on the first challenge or block, or after `max_consecutive_errors` (3) plain failures in a row.

- `"on_challenge": "stop"` (default): the target is done for the run. The last clean interval is your measured safe rate.
- `"on_challenge": "cooldown_then_hold"`: waits `cooldown_minutes`, runs one step at the last clean interval to check that it holds, then stops.

These count as a challenge or block: 429, 403, 412, 435, timeouts, Cloudflare "Just a moment" pages, PerimeterX captcha pages and 412 JSON blocks, Walmart `/blocked`, Akamai access-denied pages and HTTP/2 stream resets, and Amazon captcha and checkpoint pages. Vendor scripts that appear on normal 200 pages are not counted.

## Config

Settings live in `config.json` at the repo root. Put secrets in `config.local.json` next to it (git-ignored; objects merge key by key, lists replace), or in environment variables like `PS5_PROBE__CREDENTIALS__BESTBUY_API_KEY`.

```json
{ "credentials": {
  "bestbuy_api_key": "...",
  "walmart_consumer_id": "...", "walmart_key_version": "1", "walmart_private_key_path": "C:/keys/walmart.pem"
} }
```

A target's `ramp` overrides only the fields it sets; the rest come from `default_ramp`. API targets without credentials are skipped. Unknown setting names are an error, so typos don't pass silently.

| Target | Method | Signal | Status |
|---|---|---|---|
| `psdirect-api` | HTTP JSON | `stock.stockLevelStatus` | clean to 1s, but ~98% Akamai cache hits (`max-age=120`), so up to 2+ min stale |
| `psdirect-api-bust` | HTTP JSON, unique URL + no-cache | same | **reaches origin every time**; clean at 5s; the one to use |
| `psdirect-page` | HTTP | schema.org microdata `availability` | clean to 5s |
| `samsclub-page` | HTTP | `__NEXT_DATA__` `availabilityStatus` + `sellerType` | **PerimeterX 412 after 2 hits at 15s** on 10-16 |
| `walmart-page` | HTTP | same as Sam's; flags a redirect to another item | clean to 15s on 10-16; one 412 on 10-15 |
| `walmart-api` | Walmart.io affiliate API, RSA-signed | `availableOnline`, `stock`, `sellerInfo` | needs keys; field names unverified |
| `bestbuy-api` | Best Buy Products API | `onlineAvailability`, `orderable` | needs a key |
| `bestbuy-page` | Playwright | `data-testid="pdp-…-6601524"` button | Akamai resets both headless **and headed**; use the API |
| `target-page` | Playwright | Target buy buttons; empty buy box = out of stock | clean headless at 120s; in-stock markup unverified |
| `gamestop-page` | Playwright | New JSON-LD offer (SKU 416188); button only when a condition is selected | **headed: clean at 30s**; headless gets Cloudflare 403 |
| `newegg-page` | Playwright | `__initialState__.ItemDetail` + seller must be Newegg | **headed: clean at 30s**; headless gets Cloudflare 403 |
| `amazon-page` | Playwright | `#availability`, buy box seller | **disabled**: checkpoint on first load |

Not built: an Amazon Creators API (OffersV2) poller. Its auth and request format still need checking against the docs once there's an Associates account.

The browser targets use a fresh, throwaway Chromium profile, never your signed-in sessions, and no fingerprint spoofing.

## Layout

| File | What it does |
|---|---|
| `ps5probe/cli.py` | arguments, target selection, runs targets side by side, writes the summary |
| `ps5probe/config.py` | settings dataclasses, config loading, ramp and profile merging |
| `ps5probe/runner.py` | one target's ramp, trip and backoff logic |
| `ps5probe/fetchers.py` | plain HTTP (httpx), shared Playwright Chromium, Walmart.io signing |
| `ps5probe/parsers.py` | one stock parser per retailer |
| `ps5probe/parsing.py` | JSON, price, HTML and schema.org helpers the parsers share |
| `ps5probe/challenge.py` | bot-protection detection |
| `ps5probe/alert.py` | the would-alert rule |
| `ps5probe/log.py` | `samples.csv`, snapshots, console lines |

## Tests

```bash
pytest
```

Parser and challenge-detector tests use small fixtures shaped like what each site returned on 2026-10-15.

## Git

Commits are authored as `njm25 <njm25@users.noreply.github.com>`, unsigned. This is set in the repo's local git config.
