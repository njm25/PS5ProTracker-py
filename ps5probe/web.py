"""Web UI: browse targets, profiles and past runs, and start or stop probe runs from another machine.

Runs `python -m ps5probe` as a subprocess, one at a time, so the CLI stays the single way a run happens.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, url_for

from . import config
from .cli import _output_dir
from .notify import Notifier

app = Flask(__name__)


def _options() -> config.Options:
    return config.load()


def _runs_dir() -> Path:
    return _output_dir(_options().output_dir)


class Job:
    """The one probe subprocess the UI may have running, and its console output.

    Its pid, args and log path are kept in runs/.web/job.json, so a reloaded server (auto-reload on
    code changes) picks the run back up instead of losing track of it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.args: list[str] = []
        self.started: datetime | None = None
        self.log_path: Path | None = None
        self.exit_code: int | None = None
        self._load()

    @staticmethod
    def _state_path() -> Path:
        return _runs_dir() / ".web" / "job.json"

    def _load(self) -> None:
        try:
            state = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.pid, self.args, self.exit_code = state["pid"], state["args"], state.get("exit_code")
        self.started = datetime.fromisoformat(state["started"])
        self.log_path = Path(state["log_path"])

    def _save(self) -> None:
        self._state_path().write_text(json.dumps({
            "pid": self.pid, "args": self.args, "started": self.started.isoformat(),
            "log_path": str(self.log_path), "exit_code": self.exit_code,
        }), encoding="utf-8")

    @property
    def running(self) -> bool:
        if self.proc is not None:  # started by this process: poll() also reaps it
            if self.proc.poll() is None:
                return True
            if self.exit_code is None:
                self.exit_code = self.proc.returncode
                self._save()
            return False
        if self.pid is None or self.exit_code is not None:
            return False
        # Started before a reload: alive if the pid still belongs to a probe.
        try:
            alive = b"ps5probe" in Path(f"/proc/{self.pid}/cmdline").read_bytes()
        except OSError:
            alive = False
        return alive

    def start(self, args: list[str]) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("a run is already in progress")
            logs = _runs_dir() / ".web"
            logs.mkdir(parents=True, exist_ok=True)
            self.started = datetime.now()
            self.args = args
            self.exit_code = None
            self.log_path = logs / f"{self.started:%Y%m%d-%H%M%S}.log"
            with open(self.log_path, "w", encoding="utf-8") as out:
                # New session so SIGINT reaches only the probe, which then writes its summary,
                # and so a server reload doesn't take the run down with it.
                self.proc = subprocess.Popen(
                    [sys.executable, "-u", "-m", "ps5probe", *args],
                    cwd=config.ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                )
            self.pid = self.proc.pid
            self._save()

    def stop(self) -> None:
        if self.running:
            os.kill(self.pid, signal.SIGINT)

    def output(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path else ""
        except OSError:
            return ""

    def status(self) -> dict:
        running = self.running
        return {
            "running": running,
            "args": self.args,
            "started": self.started.isoformat(timespec="seconds") if self.started else None,
            "exit_code": self.exit_code,
            "output": self.output()[-20000:],
        }


job = Job()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _run_dir(name: str) -> Path:
    root = _runs_dir().resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_dir():
        abort(404)
    return path


def _runs() -> list[dict]:
    root = _runs_dir()
    if not root.is_dir():
        return []
    runs = []
    for d in sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")), reverse=True):
        summary = _read_csv(d / "summary.csv")
        runs.append({
            "name": d.name,
            "targets": len(summary) if summary else None,
            "samples": sum(int(r["samples"] or 0) for r in summary) if summary else None,
            "alerts": sum(int(r["would_alert"] or 0) for r in summary) if summary else None,
            "finished": bool(summary),
        })
    return runs


def _monitor() -> dict:
    """The monitor service's state file and systemd status, for the dashboard."""
    try:
        active = subprocess.run(["systemctl", "is-active", "ps5probe-monitor"], capture_output=True, text=True,
                                timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        active = "unknown"
    try:
        state = json.loads((_runs_dir() / ".monitor" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    now = time.time()

    def ago(ts: float | None) -> str:
        if not ts:
            return ""
        s = int(now - ts)
        return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago"

    rows = []
    for t in state.get("targets", {}).values():
        cooling = state.get("retailers", {}).get(t["retailer"], {}).get("cooldown_until", 0) - now
        rows.append({**t, "ago": ago(t["last_check"]), "changed_ago": ago(t["last_change"]),
                     "alert_ago": ago(t["last_alert"]), "cooling": f"{int(cooling // 60) + 1} min" if cooling > 0 else ""})
    return {"active": active, "rows": rows, "updated": ago(state.get("updated")), "webhook": state.get("webhook")}


@app.get("/")
def index():
    options = _options()
    return render_template_string(
        INDEX, options=options, runs=_runs()[:30], job=job.status(), monitor=_monitor(),
        has_display=bool(os.environ.get("DISPLAY")), notified=request.args.get("notified"),
    )


@app.post("/notify-test")
def notify_test():
    ok = Notifier(_options().discord).send_sync("Test notification from the ps5probe web UI.", title="Test")
    return redirect(url_for("index", notified="ok" if ok else "failed"))


@app.post("/run")
def start_run():
    options = _options()
    names = {t.name for t in options.targets}
    args: list[str] = []
    if request.form.get("once"):
        args.append("--once")
    if profile := request.form.get("profile"):
        if profile not in options.profiles:
            abort(400, f"unknown profile {profile}")
        args += ["--profile", profile]
    if only := request.form.getlist("only"):
        if unknown := [n for n in only if n not in names]:
            abort(400, f"unknown target(s) {', '.join(unknown)}")
        args += ["--only", ",".join(only)]
    try:
        job.start(args)
    except RuntimeError as ex:
        abort(409, str(ex))
    return redirect(url_for("index"))


@app.post("/stop")
def stop_run():
    job.stop()
    return redirect(url_for("index"))


@app.get("/api/status")
def api_status():
    return jsonify(job.status())


@app.get("/runs/<name>")
def run_detail(name: str):
    d = _run_dir(name)
    samples = _read_csv(d / "samples.csv")
    snaps = sorted((p.relative_to(d / "snapshots").as_posix() for p in (d / "snapshots").rglob("*.html")), reverse=True) \
        if (d / "snapshots").is_dir() else []
    return render_template_string(
        RUN, name=name, summary=_read_csv(d / "summary.csv"), samples=samples[-200:][::-1],
        total=len(samples), snapshots=snaps,
    )


@app.get("/runs/<name>/file/<path:rel>")
def run_file(name: str, rel: str):
    d = _run_dir(name)
    path = (d / rel).resolve()
    if not path.is_relative_to(d) or not path.is_file():
        abort(404)
    # Snapshots are third-party pages: serve as text so their scripts never run here.
    return send_file(path, mimetype="text/plain")


@app.get("/config")
def show_config():
    data = asdict(_options())
    data["credentials"] = {k: ("set" if v else None) for k, v in data["credentials"].items()}
    data["max_price"] = str(data["max_price"])
    return jsonify(data)


BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ps5probe</title><style>
:root{color-scheme:dark}
body{font:14px system-ui,sans-serif;margin:0 auto;max-width:1200px;padding:16px;background:#121316;color:#e2e2e6}
a{color:#7fb3ff}h1 a{color:inherit;text-decoration:none}h2{margin-top:28px}
table{border-collapse:collapse;width:100%;background:#1b1d22}td,th{border:1px solid #2e3139;padding:4px 8px;text-align:left;font-size:13px}
th{background:#24272e}.wrap{overflow-x:auto}.off{color:#6b6f78}.yes{color:#5fd47a;font-weight:600}.bad{color:#ff6b6b;font-weight:600}
pre{background:#0b0c0e;color:#d0d0d0;border:1px solid #2e3139;padding:10px;max-height:420px;overflow:auto;font-size:12px;white-space:pre-wrap}
button,select{background:#2a2e36;color:#e2e2e6;border:1px solid #3a3f4a;border-radius:4px;padding:4px 10px;cursor:pointer}
button:hover{background:#353a44}form.inline{display:inline}.box{background:#1b1d22;border:1px solid #2e3139;padding:12px}
</style></head><body><h1><a href="/">ps5probe</a></h1>"""

INDEX = BASE + """
<h2 style="margin-top:0">Monitor</h2>
<div class="box">
  Service: <b class="{{ 'yes' if monitor.active == 'active' else 'bad' }}">{{ monitor.active }}</b>
  {% if monitor.updated %} · state updated {{ monitor.updated }}{% endif %}
  · Discord: {{ 'webhook set' if options.discord.webhook_url else 'no webhook (console only)' }}
  <form class="inline" method="post" action="/notify-test"><button>Send test notification</button></form>
  {% if notified == 'ok' %}<span class="yes">sent</span>{% elif notified %}<span class="bad">failed: see the service log</span>{% endif %}
  {% if monitor.rows %}<div class="wrap" style="margin-top:10px"><table>
  <tr><th>target</th><th>every</th><th>last check</th><th>status</th><th>ms</th><th>in stock</th><th>signal</th><th>seller</th><th>price</th>
  <th>alert rule</th><th>checks</th><th>blocks</th><th>paused</th><th>last change</th><th>last alert</th></tr>
  {% for r in monitor.rows %}<tr>
    <td><a href="{{ r.url }}" target="_blank">{{ r.name }}</a></td><td>{{ r.interval }}s</td><td>{{ r.ago }}</td>
    <td class="{{ 'bad' if r.challenge }}">{{ r.challenge or r.status or r.error or '' }}</td><td>{{ r.latency_ms or '' }}</td>
    <td class="{{ 'yes' if r.in_stock }}">{{ r.in_stock if r.in_stock is not none else '' }}</td><td>{{ r.raw_signal or '' }}</td>
    <td>{{ r.seller or '' }}</td><td>{{ r.price or '' }}</td><td class="{{ 'yes' if r.alerting }}">{{ r.alert_reason or '' }}</td>
    <td>{{ r.checks }}</td><td class="{{ 'bad' if r.blocks }}">{{ r.blocks }}</td><td class="{{ 'bad' if r.cooling }}">{{ r.cooling }}</td>
    <td>{{ r.changed_ago }}</td><td class="{{ 'yes' if r.alert_ago }}">{{ r.alert_ago }}</td></tr>
  {% endfor %}</table></div>
  <p class="off">Checks and blocks count since the monitor last started. Notable samples: <a href="/runs/monitor">runs/monitor</a>.</p>
  {% else %}<p class="off">No monitor state yet.</p>{% endif %}
</div>

<h2>Manual probe run</h2>
<div class="box">
  <b>Run:</b> <span id="state">{{ 'running' if job.running else 'idle' }}</span>
  <span id="args">{{ job.args|join(' ') }}</span>
  <form class="inline" method="post" action="/stop"><button id="stop" {{ '' if job.running else 'hidden' }}>Stop (Ctrl+C)</button></form>
  <pre id="out">{{ job.output or 'No run started from the web UI yet.' }}</pre>
</div>

<h2>Start a run</h2>
<form method="post" action="/run" class="box">
  <label><input type="checkbox" name="once" checked> One sample each (--once)</label>
  &nbsp; Profile: <select name="profile"><option value="">(none)</option>
  {% for p, v in options.profiles.items() %}
    <option value="{{ p }}" {{ 'disabled' if v.get('headless') is false and not has_display }}>{{ p }}{{ ' (needs a display)' if v.get('headless') is false and not has_display }}</option>
  {% endfor %}</select>
  &nbsp; <button>Start</button>
  <p>Targets (none ticked = all enabled, or the profile's):</p>
  <div class="wrap"><table><tr><th></th><th>name</th><th>retailer</th><th>method</th><th>parser</th><th>intervals</th><th>url</th></tr>
  {% for t in options.targets %}
    <tr class="{{ '' if t.enabled else 'off' }}"><td><input type="checkbox" name="only" value="{{ t.name }}"></td>
    <td>{{ t.name }}{{ '' if t.enabled else ' (off)' }}</td><td>{{ t.retailer }}</td><td>{{ t.method }}</td><td>{{ t.parser }}</td>
    <td>{{ t.ramp.intervals_seconds|join(', ') }}s</td><td><a href="{{ t.product_url or t.url }}" target="_blank">link</a></td></tr>
  {% endfor %}</table></div>
</form>

<h2>Runs</h2>
<div class="wrap"><table><tr><th>run</th><th>targets</th><th>samples</th><th>would alert</th></tr>
{% for r in runs %}
  <tr><td><a href="/runs/{{ r.name }}">{{ r.name }}</a>{{ '' if r.finished else ' (in progress or no summary)' }}</td>
  <td>{{ r.targets if r.targets is not none else '' }}</td><td>{{ r.samples if r.samples is not none else '' }}</td>
  <td class="{{ 'yes' if r.alerts else '' }}">{{ r.alerts if r.alerts is not none else '' }}</td></tr>
{% else %}<tr><td colspan="4">No runs yet.</td></tr>{% endfor %}
</table></div>
<p><a href="/config">Effective config (JSON, secrets hidden)</a></p>

<script>
async function poll() {
  try {
    const s = await (await fetch('/api/status')).json();
    document.getElementById('state').textContent = s.running ? 'running' : !s.started ? 'idle' : s.exit_code === null ? 'finished' : 'finished (exit ' + s.exit_code + ')';
    document.getElementById('args').textContent = s.args.join(' ');
    document.getElementById('stop').hidden = !s.running;
    const out = document.getElementById('out');
    const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 20;
    if (s.output) out.textContent = s.output;
    if (atBottom) out.scrollTop = out.scrollHeight;
  } catch (e) {}
  setTimeout(poll, 2000);
}
poll();
</script></body></html>"""

RUN = BASE + """
<h2>{{ name }}</h2>
<p><a href="/runs/{{ name }}/file/samples.csv">samples.csv</a> · <a href="/runs/{{ name }}/file/summary.csv">summary.csv</a>
 · <a href="/runs/{{ name }}/file/targets.json">targets.json</a></p>

<h2>Summary</h2>
{% if summary %}<div class="wrap"><table><tr>{% for k in summary[0] %}<th>{{ k }}</th>{% endfor %}</tr>
{% for r in summary %}<tr>{% for v in r.values() %}<td>{{ v }}</td>{% endfor %}</tr>{% endfor %}</table></div>
{% else %}<p>No summary yet: the run is still going or was killed.</p>{% endif %}

<h2>Samples (latest {{ samples|length }} of {{ total }})</h2>
<div class="wrap"><table><tr><th>time (UTC)</th><th>target</th><th>interval</th><th>status</th><th>ms</th><th>challenge</th>
<th>parsed</th><th>in stock</th><th>signal</th><th>seller</th><th>price</th><th>alert</th><th>note / error</th></tr>
{% for s in samples %}<tr>
  <td>{{ s.timestamp_utc[11:19] }}</td><td>{{ s.target }}</td><td>{{ s.interval_s }}s</td><td>{{ s.http_status }}</td><td>{{ s.latency_ms }}</td>
  <td class="{{ 'bad' if s.challenge != 'None' }}">{{ s.challenge }}</td><td>{{ s.signal_parsed }}</td>
  <td class="{{ 'yes' if s.in_stock == 'true' }}">{{ s.in_stock }}</td><td>{{ s.raw_signal }}</td><td>{{ s.seller }}</td><td>{{ s.price }}</td>
  <td class="{{ 'yes' if s.alert_eligible == 'true' }}">{{ s.alert_reason }}</td><td>{{ s.error or s.note }}</td></tr>
{% endfor %}</table></div>

<h2>Snapshots</h2>
<ul>{% for p in snapshots %}<li><a href="/runs/{{ name }}/file/snapshots/{{ p }}">{{ p }}</a></li>{% else %}<li>None.</li>{% endfor %}</ul>
</body></html>"""


def main() -> None:
    port = int(os.environ.get("PS5_PROBE_WEB_PORT", "8080"))
    # PS5_PROBE_WEB_RELOAD=1 restarts the server whenever a ps5probe source file changes.
    reload = os.environ.get("PS5_PROBE_WEB_RELOAD", "") not in ("", "0", "false")
    app.run(host=os.environ.get("PS5_PROBE_WEB_HOST", "0.0.0.0"), port=port, threaded=True, use_reloader=reload)


if __name__ == "__main__":
    main()
