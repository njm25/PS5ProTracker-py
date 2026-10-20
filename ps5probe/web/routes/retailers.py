from flask import Blueprint, abort, render_template, send_file

from .. import data

bp = Blueprint("retailers", __name__)


def _context(name: str) -> dict:
    m = data.monitor()
    t = m.target(name)
    if t is None:
        abort(404)
    options = data.options()
    target = next((x for x in options.targets if x.name == name), None)
    return {"m": m, "t": t, "target": target, "max_price": options.max_price, "events": data.events(30, target=name),
            "snapshots": data.snapshots(name)}


@bp.get("/retailers/<name>")
def detail(name: str):
    return render_template("pages/retailer.html", **_context(name))


@bp.get("/partials/retailer/<name>")
def live(name: str):
    return render_template("partials/retailer_live.html", **_context(name))


@bp.get("/retailers/<name>/snapshots/<file>")
def snapshot(name: str, file: str):
    path = data.safe_child(data.runs_dir() / "monitor" / "snapshots", name, file)
    if path is None or not path.is_file():
        abort(404)
    # API targets save JSON, which reads better as plain text.
    with open(path, encoding="utf-8", errors="replace") as fh:
        is_json = fh.read(64).lstrip()[:1] in ("{", "[")
    resp = send_file(path, mimetype="text/plain" if is_json else "text/html")
    # A retailer's page: no scripts, no requests to the retailer from your browser, just the markup.
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    return resp
