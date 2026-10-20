# Web UI rework: plan

Status: built 2026-10-19 (all six phases). The decisions are at the end; the README's Web UI section describes the result.

## Where it is now

- One file, `ps5probe/web.py`: routes, subprocess job manager and three inline HTML templates.
- Server-rendered pages. The only live part is a 2-second poll of `/api/status` for the probe console.
- The Monitor panel is a wide table read from `runs/.monitor/state.json`. There's no history: the monitor keeps only the latest state, plus notable samples in `runs/monitor/samples.csv`.
- It can't control the monitor (check now, pause, clear a cooldown, change an interval) or edit settings.
- No auth. Flask dev server, auto-reload, port 8080, systemd `ps5probe-web`.

## Goals

1. **At a glance:** open the page on the desktop or a phone and know within a second whether anything is in stock and whether the monitor is healthy.
2. **Live:** updates arrive as they happen, without refreshing.
3. **History:** what each retailer did over the last day or week: checks, latency, blocks, signal changes.
4. **Control:** do from the browser what currently needs SSH, a config edit or sudo.
5. **Looks finished:** a consistent design system, readable on a phone.

## Tech choices

| Area | Choice | Why |
|---|---|---|
| Rendering | Jinja templates + **htmx** for partial swaps | No Node build step on the VM; pages stay server-rendered and simple |
| Live updates | **Server-Sent Events** (`/api/stream`) | One-way push is all that's needed; plain HTTP; the browser reconnects by itself |
| Charts | **uPlot** (~40 KB), vendored in `static/` | Fast with thousands of points; works without internet |
| Styling | Hand-written CSS with design tokens (CSS variables), no framework | Keeps the current dark look, but consistent |
| Server | Keep Flask's threaded server | One user on a LAN; SSE works with threads. Waitress later if needed |

## Code layout

```
ps5probe/web/
  __init__.py          create_app(), blueprint registration, `main()`
  auth.py              login, session, CSRF (see Security)
  routes/
    dashboard.py       /                     home
    retailers.py       /retailers/<name>     per-target detail
    activity.py        /activity             event feed
    runs.py            /runs, /runs/<name>   Phase 1 probe runs
    settings.py        /settings             config editor
    system.py          /system               services, logs, VM health
    api.py             /api/stream (SSE), /api/state, /api/history, /api/commands
  services/
    monitor_state.py   reads state.json, events.jsonl, checks-*.csv
    commands.py        writes commands to the monitor's inbox
    probe_job.py       today's Job class (manual probe runs)
    config_edit.py     validates and writes config.local.json
    system_info.py     systemctl status, journal tail, memory/CPU/disk
  templates/
    base.html          layout, nav, toast area, SSE hookup
    partials/          retailer_card, event_row, status_pill, hero, ...
    pages/             one per route
  static/
    css/app.css        tokens + components
    js/app.js          SSE client, countdowns, sound/title alerts
    vendor/            htmx, uPlot
```

The current `ps5probe/web.py` turns into this package; `python -m ps5probe.web` and the service file stay as they are.

## Monitor changes the UI needs

The UI can only show history the monitor records, and can only control the monitor through a channel it listens on.

1. **Event log** `runs/.monitor/events.jsonl`: one line per alert, stock gone, signal change, block, resume, start, stop, reload and heartbeat. Each line records whether Discord accepted it. This feeds the activity feed and the retailer timelines.
2. **Check history** `runs/.monitor/checks-YYYYMMDD.csv`: one compact row per check (time, target, HTTP status, latency, in_stock, challenge, error). About 0.5 MB/day at the current rates; deleted after 14 days. This feeds the charts and uptime percentages.
3. **Command inbox** `runs/.monitor/commands/`: the UI drops one JSON file per command, and the monitor's existing 3-second watcher picks it up, runs it and deletes it. Commands:
   - `check_now <target>`
   - `pause <target> [minutes]` / `resume <target>`
   - `clear_cooldown <retailer>`

   A file inbox needs no new port or socket, and it survives the monitor's reloads.
4. **Next check time** per target in `state.json`, so cards can count down.

## Pages

### Dashboard `/`
- **Hero banner**, the one thing to read:
  - Normal: "Nothing in stock · 7 retailers · last check 4s ago".
  - Restock: big and green, "IN STOCK at GameStop · $899.99 · Buy now".
  - Problem: "Monitor down since 14:02" in red.
- **Retailer cards** in a grid, one per target:
  - status pill (in stock / out of stock / paused / blocked / error)
  - price and seller
  - a ring counting down to the next check
  - a 1-hour latency sparkline and 24-hour uptime %
  - quick actions: check now, pause
- **Activity feed**: the latest 20 events, with new ones arriving live.
- **Health strip**:
  - monitor and web services
  - whether the Discord webhook works
  - Chromium/Xvfb up
  - VM memory, CPU, disk
  - monitor uptime

### Retailer detail `/retailers/<name>`
- A 24-hour (or 7-day) status timeline: a coloured strip with OK / blocked / error / in-stock spans.
- Latency chart, block history, and that retailer's events.
- **Latest snapshot**: the saved page shown in a sandboxed iframe with scripts blocked, so you can see what the parser saw.
- Settings for that target (interval, enabled, allowed sellers), plus the actions.

### Activity `/activity`
- Every event, filterable by type and retailer, paged.
- Shows whether each one reached Discord.

### Runs `/runs` (Phase 1 tool)
- A start-run form: profile, targets, once.
- Live console over SSE.
- Run detail with charts: latency per ramp step, and where it tripped.

### Settings `/settings`
- Monitor intervals, which targets run, max price.
- Discord: mention, toggles, webhook (masked), test button.
- Cooldown and heartbeat settings.
- Saving validates with `config.from_dict` before writing `config.local.json`. The monitor already reloads when that file changes, so a save goes live within seconds.

### System `/system`
- Service status and the latest journal lines for both services.
- Restart buttons. These need a one-time sudoers rule that allows only `systemctl restart ps5probe-monitor` / `ps5probe-web`.

## Alerts in the browser
- A tab title change and favicon colour on a restock, for example "🟢 IN STOCK".
- An optional sound. Browsers require one click on the page first.
- **Gotcha:** desktop notifications and installing the page as a phone app both need HTTPS. Over plain `http://192.168.0.115` they won't work. `tailscale serve` gives the VM an HTTPS address on your tailnet, which fixes this for free.

## Security
Once the UI can edit settings and control the monitor, anyone on the LAN could do that. Options:
- a single password from `config.local.json`, with a session cookie and CSRF tokens on forms (recommended), or
- only allowing access over Tailscale.

## Build order

Each phase is shippable on its own.

1. **Restructure.** Package, blueprints, templates, static files, base layout, design tokens. Same features. Add Flask test-client tests.
2. **Monitor data.** Event log, check history with pruning, command inbox, next-check time. Unit tests.
3. **Live dashboard.** SSE stream, hero banner, retailer cards, activity feed, health strip.
4. **Retailer detail and activity.** Charts, timelines, snapshot viewer, filters.
5. **Control.** Command buttons, settings editor, login.
6. **Polish.** Run pages, browser alerts and sound, phone layout, optional Tailscale HTTPS, System page.

## Decisions (2026-10-19)
1. **No login.** Access is over the LAN or Tailscale, and that's enough.
2. **Phone friendly:** yes.
3. **Sudoers rule** for the restart buttons: yes.
4. **No browser notifications or HTTPS.** Discord handles alerts; the UI is for managing the service. The "Alerts in the browser" section is dropped, and so is the login in phase 5.
