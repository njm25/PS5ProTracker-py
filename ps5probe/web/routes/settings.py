from flask import Blueprint, redirect, render_template, request, url_for

from .. import control, data, toast
from ...notify import Notifier

bp = Blueprint("settings", __name__)


def _render(errors: list[str] | None = None, form: dict | None = None):
    options = data.options()
    return render_template(
        "pages/settings.html", options=options, errors=errors or [], form=form,
        saved=request.args.get("saved"), mask=control.SECRET_MASK, min_interval=control.MIN_INTERVAL,
    )


@bp.get("/settings")
def index():
    return _render()


@bp.post("/settings")
def save():
    targets = [t.name for t in data.options().targets]
    errors = control.save_settings(request.form, targets)
    if errors:
        return _render(errors, request.form), 400
    return redirect(url_for("settings.index", saved=1))


@bp.post("/settings/test-notify")
def test_notify():
    ok = Notifier(data.options().discord).send_sync("Test notification from the ps5probe web UI.", title="Test")
    return toast("Test message sent to Discord" if ok else "Discord didn't take it: check the webhook", ok=ok,
                 refresh=False)

