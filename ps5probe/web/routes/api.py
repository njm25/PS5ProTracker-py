import time

from flask import Blueprint, Response, abort, jsonify, request

from .. import control, data, toast
from ..job import job

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.get("/stream")
def stream():
    """Server-sent events: `monitor` when the monitor's files change, `job` when a probe run's output does."""
    folder = data.monitor_dir()

    def signature(*paths) -> tuple:
        return tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)

    def events():
        yield "retry: 3000\n\n"
        last_monitor = last_job = None
        last_sent = last_ping = 0.0
        while True:
            now = time.monotonic()
            mon = signature(folder / "state.json", folder / "events.jsonl")
            # The monitor saves every few seconds; one refresh per 3s is plenty.
            if mon != last_monitor and now - last_sent >= 3:
                if last_monitor is not None:
                    yield "event: monitor\ndata: {}\n\n"
                last_monitor, last_sent = mon, now
            j = (job.running, signature(job.log_path) if job.log_path else None)
            if j != last_job:
                if last_job is not None:
                    yield "event: job\ndata: {}\n\n"
                last_job = j
            if now - last_ping >= 15:  # keeps proxies happy and notices closed tabs
                yield ": ping\n\n"
                last_ping = now
            time.sleep(1)

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.get("/state")
def state():
    m = data.monitor()
    return jsonify({
        "service": m.service, "healthy": m.healthy, "updated": m.updated,
        "targets": [{**t.raw, "kind": t.kind, "label": t.label, "stats": t.stats} for t in m.targets],
        "retailers": m.retailers,
    })


@bp.get("/history/<target>")
def history(target: str):
    """Columns for uPlot: time, latency of good checks, latency of failed ones."""
    hours = min(request.args.get("hours", 24, type=float), 24 * 14)
    rows = data.checks(hours, target)
    return jsonify({
        "t": [r[0] for r in rows],
        "ok": [r[3] if r[4] is not None else None for r in rows],
        "bad": [r[3] or 0 if r[4] is None else None for r in rows],
        "status": [r[5] or r[6] or r[2] for r in rows],
        "in_stock": [r[4] for r in rows],
    })


@bp.post("/command")
def command():
    cmd = request.form.get("cmd", "")
    args = {k: request.form[k] for k in ("target", "retailer", "minutes") if request.form.get(k)}
    if cmd in ("check_now", "pause", "resume") and "target" not in args:
        abort(400)
    if cmd == "clear_cooldown" and "retailer" not in args:
        abort(400)
    try:
        control.command(cmd, **args)
    except ValueError as ex:
        return toast(str(ex), ok=False, refresh=False)
    if not data.monitor(with_stats=False).healthy:
        return toast("Queued, but the monitor isn't running: it'll happen when it starts", ok=False)
    what = {"check_now": "Checking", "pause": "Paused", "resume": "Resumed", "clear_cooldown": "Cooldown cleared:"}
    return toast(f"{what[cmd]} {args.get('target') or args.get('retailer')}")
