import os

from flask import Blueprint, abort, render_template, request, send_file

from .. import data, toast
from ..job import job

bp = Blueprint("runs", __name__)


@bp.get("/runs")
def index():
    options = data.options()
    return render_template("pages/runs.html", runs=data.runs()[:60], options=options, job=job.status(),
                           has_display=bool(os.environ.get("DISPLAY")))


@bp.get("/partials/job")
def live_job():
    return render_template("partials/job.html", job=job.status())


@bp.post("/runs/start")
def start():
    options = data.options()
    names = {t.name for t in options.targets}
    args: list[str] = []
    if request.form.get("once"):
        args.append("--once")
    if profile := request.form.get("profile"):
        if profile not in options.profiles:
            return toast(f"Unknown profile {profile}", ok=False, refresh=False)
        args += ["--profile", profile]
    if only := request.form.getlist("only"):
        if unknown := [n for n in only if n not in names]:
            return toast(f"Unknown target(s) {', '.join(unknown)}", ok=False, refresh=False)
        args += ["--only", ",".join(only)]
    try:
        job.start(args)
    except RuntimeError as ex:
        return toast(str(ex), ok=False, refresh=False)
    return toast("Probe run started", refresh=False, also=("job",))


@bp.post("/runs/stop")
def stop():
    job.stop()
    return toast("Stopping (the summary is still written)", refresh=False, also=("job",))


def _run_dir(name: str):
    path = data.safe_child(data.runs_dir(), name)
    if path is None or path.parent != data.runs_dir().resolve() or not path.is_dir():
        abort(404)
    return path


@bp.get("/runs/<name>")
def detail(name: str):
    d = _run_dir(name)
    samples = data.read_csv(d / "samples.csv")
    snaps_root = d / "snapshots"
    snaps = sorted((p.relative_to(snaps_root).as_posix() for p in snaps_root.rglob("*.html")), reverse=True) \
        if snaps_root.is_dir() else []
    return render_template(
        "pages/run.html", name=name, summary=data.read_csv(d / "summary.csv"), samples=samples[-300:][::-1],
        total=len(samples), snapshots=snaps[:200],
        chart=[{"t": s["timestamp_utc"], "target": s["target"], "ms": s["latency_ms"]} for s in samples[-3000:]],
    )


@bp.get("/runs/<name>/file/<path:rel>")
def file(name: str, rel: str):
    path = data.safe_child(_run_dir(name), rel)
    if path is None or not path.is_file():
        abort(404)
    # Snapshots are third-party pages: served as text so their scripts never run here.
    return send_file(path, mimetype="text/plain")
