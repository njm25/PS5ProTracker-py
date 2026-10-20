from flask import Blueprint, abort, render_template

from .. import data, system, toast

bp = Blueprint("system", __name__)


def _context() -> dict:
    return {"units": [system.unit(u) for u in system.UNITS], "vm": system.vm(), "sudo_ready": system.sudo_ready(),
            "m": data.monitor(with_stats=False), "proxy_list": data.proxy_list(), "proxy_cap": data.options().proxies.daily_mb}


@bp.get("/system")
def index():
    return render_template("pages/system.html", **_context())


@bp.get("/partials/dot")
def dot():
    """The monitor-health dot in the top bar."""
    healthy = data.monitor(with_stats=False).healthy
    return render_template("partials/dot.html", healthy=healthy)


@bp.get("/partials/system")
def live():
    return render_template("partials/system_live.html", **_context())


@bp.get("/partials/journal/<unit>")
def journal(unit: str):
    if unit not in system.UNITS:
        abort(404)
    return render_template("partials/journal.html", text=system.journal(unit))


@bp.post("/system/<unit>/<action>")
def control(unit: str, action: str):
    ok, message = system.control(unit, action)
    return toast(message, ok=ok)
