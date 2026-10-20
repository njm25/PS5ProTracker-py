from flask import Blueprint, render_template, request

from .. import data

bp = Blueprint("activity", __name__)
PAGE = 50


def _context() -> dict:
    kind = request.args.get("kind") or None
    target = request.args.get("target") or None
    before = request.args.get("before", type=float)
    events = data.events(PAGE + 1, kind=kind, target=target, before=before)
    return {
        "events": events[:PAGE],
        "more": events[PAGE - 1]["ts"] if len(events) > PAGE else None,
        "kind": kind, "target": target, "before": before,
        "kinds": data.EVENT_KINDS,
        "targets": sorted(data.options().monitor.targets),
    }


@bp.get("/activity")
def index():
    return render_template("pages/activity.html", **_context())


@bp.get("/partials/activity")
def live():
    return render_template("partials/activity_list.html", **_context())
