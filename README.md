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
python -m ps5probe --monitor            # poll monitor.targets forever, post to Discord
python -m ps5probe --test-notify        # post a test message to the Discord webhook
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

## Web UI

`ps5probe-web` (or `python -m ps5probe.web`) serves the management UI on `0.0.0.0:8080`. It's meant for the LAN or Tailscale only and has no login. It works on a phone.

- **Dashboard:**
  - a banner saying whether anything is in stock (with buy links) or the monitor is down
  - one card per target: status, price and seller, raw signal, a countdown to the next check, a 1-hour latency sparkline, 24-hour coverage (the share of the day a good check vouched for, counting time paused after blocks) and median latency
  - buttons on each card: **check now**, **pause** (1 hour, or until resumed), **clear cooldown**
  - a health strip and the latest activity
- **Retailer pages:** a 6h / 24h / 7-day status strip and latency chart, that target's events, and the setup. Saved pages are shown in a sandboxed frame with scripts and outside requests blocked.
- **Activity:** every monitor event, filterable by kind and target, with whether Discord took it.
- **Probe runs:** start or stop a Phase 1 run with a live console, and browse past runs with a latency chart, samples and snapshots.
- **Settings:** which targets the monitor watches and how often, max price, Discord mention, webhook and toggles, cooldowns, jitter, heartbeat. Saving validates the settings and writes `config.local.json` (mode 600); the monitor reloads within seconds.
- **System:** both services' status, memory and restarts, start / stop / restart buttons, live journal tails, and VM memory, load, disk, Chromium and Xvfb.

Pages update live over server-sent events (`/api/stream`). Front end: Jinja templates, htmx, and uPlot for charts, vendored in `ps5probe/web/static/vendor/`, with no build step.

`PS5_PROBE_WEB_HOST` / `PS5_PROBE_WEB_PORT` change the bind address. `PS5_PROBE_WEB_RELOAD=1` (set in the service) restarts the server whenever a `ps5probe` source file changes; a probe run in progress carries on and is picked back up. Templates and static files reload without it.

```bash
pip install -e ".[web]"
sudo cp deploy/ps5probe-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ps5probe-web
# Optional, for the System page's start/stop/restart buttons (allows only those exact commands):
sudo install -m 440 deploy/ps5probe.sudoers /etc/sudoers.d/ps5probe && sudo visudo -c
```

## Monitor (Phase 2)

`python -m ps5probe --monitor` polls the targets in `monitor.targets` (name → seconds; 0 turns one off) forever and posts to a Discord webhook:

- **Stock alert** (pings `discord.mention`, default `@here`): a target starts meeting the alert rule. Sent once per restock, even across restarts.
- **Stock gone**: an alerting target stops meeting the rule.
- **Signal changed** (`notify_changes`): the stock signal, seller or price changed without meeting the rule. Nothing has been seen in stock yet, so this is the safety net for a parser misreading a restock.
- **Blocked / resumed** (`notify_blocks`): a bot challenge pauses every target of that retailer (it's about our IP); `max_consecutive_errors` timeouts or errors in a row pause only that target. Either lasts `cooldown_minutes`, doubling on each further block up to `max_cooldown_minutes`. A clean hour resets it. PS Direct's API stops answering after about 480 requests (80 min at 10s, 10-20), hence its 30s interval.
- **Started / stopped**, and a **still running** summary every `heartbeat_hours`.

Put the webhook in `config.local.json` (`{"discord": {"webhook_url": "https://discord.com/api/webhooks/..."}}`) and check it with `python -m ps5probe --test-notify` or the web UI button. Without one, messages only go to the console.

Everything the UI shows lives in `runs/.monitor/`:
- `state.json`: last signal, cooldowns, pauses, what already alerted.
- `events.jsonl`: alerts, changes, blocks, commands.
- `checks-YYYYMMDD.csv`: one row per check, kept 14 days.
- `commands/`: the UI drops check-now, pause, resume and clear-cooldown requests here.

A lone timeout counts as a failure, not a block. Notable samples and their snapshots go to `runs/monitor/`. The monitor re-executes itself when `ps5probe/*.py` or the config changes. Run it as a service:

```bash
sudo cp deploy/ps5probe-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ps5probe-monitor
journalctl -u ps5probe-monitor -f
```

### Proxies

Settings → Proxies takes a list URL (e.g. Webshare's download link: one `ip:port:user:pass` per line; kept in `config.local.json`). The monitor caches the list in `runs/.monitor/proxies.json` (mode 600) and re-downloads it every `refresh_hours`. Each target picks **Direct**, **Rotate** (a different proxy per check; HTTP targets) or **Same proxy** (keeps one, so cookies stay consistent, until it's blocked). Through a proxy, a block rests only that proxy for that retailer (`proxies.cooldown_minutes`, doubling), three failures in a row rest it too, and the retailer keeps being checked through the others. A proxied browser skips images, fonts and video. The System page shows each proxy's checks, blocks, MB today and rests.

Tested 10-20 with 20 Webshare static residential proxies (US home ISPs):

| Target | Through the proxies |
|---|---|
| `psdirect-api-bust` | 20/20 clean, ~0.8s, ~7 KB a check |
| `samsclub-page` (browser) | 3/3 clean (the VM's own IP gets "not a robot"); ~5 MB a check even with images skipped |
| `walmart-page` | blocked: 20/20 HTTP and 2/2 browser (PerimeterX knows these IPs) |
| `bestbuy-page` (browser) | blocked 3/3: Akamai resets the browser regardless of IP |

## How the ramp and backoff work

Each target walks its `intervals_seconds` from slowest to fastest, taking `samples_per_step` samples at each step (±15% jitter). A target **trips** on the first challenge or block, or after `max_consecutive_errors` (3) plain failures in a row.

- `"on_challenge": "stop"` (default): the target is done for the run. The last clean interval is your measured safe rate.
- `"on_challenge": "cooldown_then_hold"`: waits `cooldown_minutes`, runs one step at the last clean interval to check that it holds, then stops.

These count as a challenge or block: 429, 403, 412, 435, timeouts, Cloudflare "Just a moment" pages, PerimeterX captcha pages and 412 JSON blocks, Walmart `/blocked`, Akamai access-denied pages and HTTP/2 stream resets, and Amazon captcha and checkpoint pages. Vendor scripts that appear on normal 200 pages are not counted.

## Config

Settings live in `config.json` at the repo root. Put secrets (the Discord webhook) in `config.local.json` next to it (git-ignored; objects merge key by key, lists replace), or in environment variables like `PS5_PROBE__DISCORD__WEBHOOK_URL`. The web UI's Settings page writes that file.

A target's `ramp` overrides only the fields it sets; the rest come from `default_ramp`. Unknown setting names are an error, so typos don't pass silently.

| Target | Method | Signal | Status |
|---|---|---|---|
| `psdirect-api` | HTTP JSON | `stock.stockLevelStatus` | clean to 1s, but ~98% Akamai cache hits (`max-age=120`), so up to 2+ min stale |
| `psdirect-api-bust` | HTTP JSON, unique URL + no-cache | same | **reaches origin every time**; clean at 5s; the one to use |
| `psdirect-page` | HTTP | schema.org microdata `availability` | clean to 5s |
| `samsclub-page` | Playwright | `__NEXT_DATA__` `availabilityStatus` + `sellerType` | HTTP: **PerimeterX 412 after 2 hits at 15s** on 10-16, and after 5–11 hits at 180s in the monitor (7% coverage). Moved to the browser 10-20; its "not a robot" page is a 200 there |
| `walmart-page` | HTTP | same as Sam's; flags a redirect to another item | clean to 15s on 10-16; one 412 on 10-15 |
| `bestbuy-page` | Playwright | `data-testid="pdp-…-6601524"` button | Akamai resets both headless **and headed**. The official Products API and Walmart's affiliate API were removed on 10-20: Best Buy turns down free email sign-ups and Walmart's is for affiliates only |
| (no target) | HTTP `/api/3.0/priceBlocks?skus=` | `buttonState` | **not usable**: answered without a key on 10-20 but said `ADD_TO_CART` while the page showed Sold Out; about a dozen requests to it and `button-state` got the VM's IP reset by Akamai |
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
| `ps5probe/fetchers.py` | plain HTTP (httpx), shared Playwright Chromium, either through a proxy |
| `ps5probe/proxies.py` | proxy list download and cache, the pool: rotate/sticky picks and per-retailer rests |
| `ps5probe/parsers.py` | one stock parser per retailer |
| `ps5probe/parsing.py` | JSON, price, HTML and schema.org helpers the parsers share |
| `ps5probe/challenge.py` | bot-protection detection |
| `ps5probe/alert.py` | the would-alert rule |
| `ps5probe/log.py` | `samples.csv`, snapshots, console lines |
| `ps5probe/monitor.py` | Phase 2: polls forever, cooldowns, Discord events |
| `ps5probe/notify.py` | Discord webhook posts |
| `ps5probe/web/` | Flask UI: `data.py` reads monitor/run files, `control.py` sends commands and saves settings, `system.py` talks to systemd, `routes/` one module per page, `templates/`, `static/` |

## Tests

```bash
pytest
```

Parser and challenge-detector tests use small fixtures shaped like what each site returned on 2026-10-15.

## Git

Commits are authored as `njm25 <njm25@users.noreply.github.com>`, unsigned. This is set in the repo's local git config.
