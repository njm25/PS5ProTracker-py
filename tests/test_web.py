import json
import time

import pytest

from ps5probe import config
from ps5probe.web import app, control, data, system


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app against an empty runs dir, a fake systemd and a private config.local.json."""
    monkeypatch.setattr(data, "runs_dir", lambda: tmp_path)
    monkeypatch.setattr(data, "monitor_dir", lambda: tmp_path / ".monitor")
    monkeypatch.setattr(control, "monitor_dir", lambda: tmp_path / ".monitor")
    monkeypatch.setattr(control, "LOCAL", tmp_path / "config.local.json")
    monkeypatch.setattr(system, "unit_state", lambda unit: "active")
    app.config["TESTING"] = True
    return app.test_client()


def write_state(tmp_path, **target):
    folder = tmp_path / ".monitor"
    folder.mkdir(exist_ok=True)
    t = {"name": "ps", "retailer": "PS Direct", "interval": 10, "url": "https://shop.example/p", "checks": 3,
         "blocks": 0, "last_check": time.time() - 5, "next_check": time.time() + 5, "status": 200, "latency_ms": 120,
         "challenge": None, "error": None, "signal_found": True, "in_stock": False, "raw_signal": "stock=out",
         "seller": "PS Direct", "price": "899.0", "alert_reason": "not in stock", "consecutive_errors": 0,
         "paused_until": 0, "signal_key": "k", "alerting": False, "last_change": None, "last_alert": None, **target}
    state = {"pid": 1, "started": time.time() - 60, "updated": time.time(), "webhook": True,
             "targets": {"ps": t}, "retailers": {"PS Direct": {"strikes": 0, "cooldown_until": 0}}}
    (folder / "state.json").write_text(json.dumps(state))
    (folder / "events.jsonl").write_text(json.dumps(
        {"ts": time.time(), "kind": "change", "target": "ps", "retailer": "PS Direct", "title": "changed",
         "text": "Was: x\nNow: y", "ping": False, "delivered": True}) + "\n")


@pytest.mark.parametrize("path", ["/", "/activity", "/activity?kind=change", "/runs", "/settings", "/system",
                                  "/partials/dashboard", "/partials/activity", "/partials/dot", "/partials/job",
                                  "/api/state"])
def test_pages_render_with_no_monitor_state(client, path):
    assert client.get(path).status_code == 200


def test_dashboard_shows_targets_and_in_stock_banner(client, tmp_path):
    write_state(tmp_path)
    page = client.get("/").get_data(as_text=True)
    assert "Nothing in stock" in page and "PS Direct" in page and "$899.00" in page

    write_state(tmp_path, alerting=True, in_stock=True)
    page = client.get("/").get_data(as_text=True)
    assert "IN STOCK" in page and "Buy at PS Direct" in page


def test_retailer_page_and_unknown_target(client, tmp_path):
    write_state(tmp_path)
    assert client.get("/retailers/ps").status_code == 200
    assert client.get("/retailers/nope").status_code == 404


def test_command_lands_in_the_monitor_inbox(client, tmp_path):
    write_state(tmp_path)
    r = client.post("/api/command", data={"cmd": "pause", "target": "ps", "minutes": "60"})
    assert r.status_code == 200 and "toast" in r.headers["HX-Trigger"]
    [queued] = (tmp_path / ".monitor" / "commands").glob("*.json")
    assert json.loads(queued.read_text()) == {"cmd": "pause", "target": "ps", "minutes": "60"}
    assert client.post("/api/command", data={"cmd": "pause"}).status_code == 400
    assert "unknown command" in client.post("/api/command", data={"cmd": "rm", "target": "ps"}).headers["HX-Trigger"]


def test_history_api(client, tmp_path):
    folder = tmp_path / ".monitor"
    folder.mkdir()
    now = time.time()
    (folder / f"checks-{time.strftime('%Y%m%d')}.csv").write_text(
        "ts,target,status,latency_ms,in_stock,challenge,error\n"
        f"{now - 20:.0f},ps,200,100,0,,\n{now - 10:.0f},ps,412,50,,WalmartBlock,\n{now - 5:.0f},other,200,1,0,,\n")
    d = client.get("/api/history/ps?hours=1").get_json()
    assert d["ok"] == [100, None] and d["bad"] == [None, 50] and d["status"][1] == "WalmartBlock"


def test_snapshot_and_run_files_cannot_escape(client, tmp_path):
    (tmp_path / "monitor" / "snapshots" / "ps").mkdir(parents=True)
    (tmp_path / "monitor" / "snapshots" / "ps" / "a-first.html").write_text("{\"x\": 1}")
    r = client.get("/retailers/ps/snapshots/a-first.html")
    assert r.status_code == 200 and r.mimetype == "text/plain" and "sandbox" in r.headers["Content-Security-Policy"]
    assert client.get("/retailers/ps/snapshots/..%2F..%2F..%2Fstate.json").status_code == 404
    assert client.get("/runs/..%2F..").status_code == 404
    assert client.get("/runs/monitor/file/..%2F..%2Fconfig.json").status_code == 404


def settings_form(**over):
    form = {"max_price": "899.99", "mention": "@here", "notify_changes": "on", "notify_blocks": "on",
            "webhook_url": control.SECRET_MASK, "cooldown_minutes": "30", "max_cooldown_minutes": "240",
            "jitter": "0.15", "heartbeat_hours": "24", "on_psdirect-api-bust": "on", "interval_psdirect-api-bust": "15"}
    return {**form, **over}


def test_settings_save_writes_local_config(client, tmp_path):
    r = client.post("/settings", data=settings_form())
    assert r.status_code == 302
    local = json.loads((tmp_path / "config.local.json").read_text())
    assert local["monitor"]["targets"]["psdirect-api-bust"] == 15
    assert local["monitor"]["targets"]["walmart-page"] == 0  # unticked: off, overriding config.json
    assert "webhook_url" not in local["discord"]  # the mask means "keep what's there"
    assert (tmp_path / "config.local.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("over, message", [
    ({"interval_psdirect-api-bust": "2"}, "at least 5"),
    ({"on_psdirect-api-bust": ""}, "at least one target"),
    ({"max_price": "abc"}, "a number"),
    ({"webhook_url": "https://example.com/hook"}, "discord.com/api/webhooks"),
    ({"cooldown_minutes": "300"}, "Max cooldown"),
])
def test_settings_rejects_bad_values(client, tmp_path, over, message):
    r = client.post("/settings", data=settings_form(**over))
    assert r.status_code == 400 and message in r.get_data(as_text=True)
    assert not (tmp_path / "config.local.json").exists()


def test_service_control_is_limited_to_its_own_units():
    assert system.control("sshd", "restart") == (False, "restart sshd is not allowed")
    assert system.control("ps5probe-web", "stop")[0] is False


def test_saved_settings_still_load():
    # Sanity: the shipped config plus an "everything off but one" local file is valid.
    raw = config._merge(config._read(config.ROOT / "config.json"), {"monitor": {"targets": {"walmart-page": 0}}})
    assert "walmart-page" not in config.from_dict(raw).monitor.targets


def test_coverage_counts_time_paused_after_a_block():
    # Good checks every 10s for 10 min, then nothing for 10 min: about half covered.
    now = 1_000_000.0
    good = [now - 1200 + i * 10 for i in range(60)]
    assert data._coverage(good, good[0], now, 10) == 52  # the last check vouches for 30s more
    assert data._coverage(good, good[0], now, None) is None
