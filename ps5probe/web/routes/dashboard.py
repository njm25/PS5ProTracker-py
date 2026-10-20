from flask import Blueprint, render_template

from .. import data

bp = Blueprint("dashboard", __name__)


def _context() -> dict:
    return {"m": data.monitor(), "events": data.events(12)}


@bp.get("/")
def index():
    return render_template("pages/dashboard.html", **_context())


@bp.get("/partials/dashboard")
def live():
    return render_template("partials/dashboard_live.html", **_context())
