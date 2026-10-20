from flask import Blueprint, redirect, render_template, request, url_for

from .. import control, data, toast
from ...notify import Notifier

bp = Blueprint("settings", __name__)


def _render(errors: list[str] | None = None, form: dict | None = None):
    options = data.options()
    api_targets = [t for t in options.targets if t.method in control.API_METHODS]
    return render_template(
        "pages/settings.html", options=options, errors=errors or [], form=form,
        saved=request.args.get("saved"), mask=control.SECRET_MASK, min_interval=control.MIN_INTERVAL,
        api_targets=api_targets, missing={t.name: control.missing_credentials(t, options.credentials)
                                          for t in api_targets},
        walmart_public_key=control.public_key(options.credentials.walmart_private_key_path),
        key_generated=request.args.get("key"),
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


@bp.post("/settings/walmart-key")
def walmart_key():
    control.generate_walmart_key()
    return redirect(url_for("settings.index", key=1, _anchor="api-keys"))


@bp.post("/settings/test-api/<name>")
def test_api(name: str):
    ok, message = control.test_api(name)
    return toast(f"{name}: {message}", ok=ok, refresh=False)
