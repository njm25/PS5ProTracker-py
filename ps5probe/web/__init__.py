"""Web UI for managing the monitor and the probe: live dashboard, per-retailer history, activity,
settings, services and Phase 1 probe runs.

No login: it's reached over the LAN or Tailscale only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

from flask import Flask, Response
from markupsafe import Markup, escape

from . import data


def create_app() -> Flask:
    # Live panels poll and stream constantly: per-request lines would bury the useful log lines.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = Flask(__name__)
    app.json.sort_keys = False
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # template edits show up without a server reload

    from .routes import activity, api, dashboard, retailers, runs, settings, system

    for module in (dashboard, retailers, activity, runs, settings, system, api):
        app.register_blueprint(module.bp)

    app.jinja_env.filters.update(ago=ago, clock=clock, duration=duration, money=money)
    app.jinja_env.globals.update(sparkline=data.sparkline, now=time.time)

    @app.context_processor
    def nav() -> dict:
        return {"nav": [("dashboard.index", "Dashboard"), ("activity.index", "Activity"), ("runs.index", "Probe runs"),
                        ("settings.index", "Settings"), ("system.index", "System")]}

    return app


def toast(message: str, ok: bool = True, status: int = 200, refresh: bool = True,
          also: tuple[str, ...] = ()) -> Response:
    """An empty htmx response that pops a toast and refreshes the live panels (monitor, plus `also`)."""
    trigger: dict = {"toast": {"message": message, "ok": ok}}
    for event in ("monitor",) * refresh + also:
        trigger[event] = True
    return Response("", status=status, headers={"HX-Trigger": json.dumps(trigger)})


# --- template filters ---

def ago(ts: float | None) -> Markup:
    """Relative time that the page's JS keeps ticking."""
    if not ts:
        return Markup('<span class="muted">never</span>')
    return Markup(f'<time data-ago="{ts:.0f}" title="{escape(clock(ts, True))}">{escape(_ago_text(ts))}</time>')


def _ago_text(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 0:
        return "just now"
    return f"{s}s ago" if s < 90 else f"{s // 60}m ago" if s < 5400 else f"{s // 3600}h ago" if s < 172800 \
        else f"{s // 86400}d ago"


def clock(ts: float | None, full: bool = False) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    if full or dt.date() != datetime.now().date():
        return dt.strftime("%b %d %H:%M:%S")
    return dt.strftime("%H:%M:%S")


def duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s" if s < 600 else f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def money(v: str | float | None) -> str:
    if v in (None, ""):
        return ""
    try:
        return f"${float(v):,.2f}"
    except ValueError:
        return str(v)


app = create_app()


def main() -> None:
    port = int(os.environ.get("PS5_PROBE_WEB_PORT", "8080"))
    # PS5_PROBE_WEB_RELOAD=1 restarts the server whenever a ps5probe source file changes.
    reload = os.environ.get("PS5_PROBE_WEB_RELOAD", "") not in ("", "0", "false")
    app.run(host=os.environ.get("PS5_PROBE_WEB_HOST", "0.0.0.0"), port=port, threaded=True, use_reloader=reload)
