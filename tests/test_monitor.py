import asyncio

import pytest

from ps5probe import config
from ps5probe.models import FetchResult
import json
import time

from ps5probe.monitor import PAUSED_FOREVER, Monitor
from ps5probe.parsers import PARSERS


def stock_json(status, price=899):
    return '{"products":[{"price":{"value":%s},"stock":{"stockLevelStatus":"%s"}}]}' % (price, status)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append({"text": text, **kwargs})
        return True


@pytest.fixture
def monitor(tmp_path):
    options = config.from_dict({
        "output_dir": str(tmp_path),
        "monitor": {"targets": {"ps": 10}, "cooldown_minutes": 30, "max_cooldown_minutes": 60},
        "targets": [{
            "name": "ps", "retailer": "PS Direct", "parser": "psdirect-api", "url": "https://shop.example/p",
            "allowed_sellers": ["PlayStation Direct"],
        }],
    })
    m = Monitor(options)
    m.notifier = FakeNotifier()
    yield m
    m.log.close()


def sample(m, status, body):
    async def fetch():
        return FetchResult(status, 5, body, "https://shop.example/p")

    t = m.targets[0]
    asyncio.run(m._sample(t, m.states[t.name], fetch, PARSERS[t.parser]))


def test_alerts_once_when_stock_appears_then_reports_it_gone(monitor):
    sample(monitor, 200, stock_json("outOfStock"))
    assert monitor.notifier.sent == []

    sample(monitor, 200, stock_json("inStock"))
    sample(monitor, 200, stock_json("inStock"))
    pings = [s for s in monitor.notifier.sent if s.get("ping")]
    assert len(pings) == 1
    assert "IN STOCK" in pings[0]["title"]

    sample(monitor, 200, stock_json("outOfStock"))
    assert "stock gone" in monitor.notifier.sent[-1]["title"]
    assert not monitor.notifier.sent[-1].get("ping")


def test_signal_change_that_does_not_alert_is_a_quiet_message(monitor):
    sample(monitor, 200, stock_json("outOfStock"))
    sample(monitor, 200, stock_json("outOfStock", price=949))
    assert len(monitor.notifier.sent) == 1
    assert "changed" in monitor.notifier.sent[0]["title"]
    assert not monitor.notifier.sent[0].get("ping")


def test_blocks_pause_the_retailer_and_back_off(monitor):
    retailer = monitor.retailers["PS Direct"]
    sample(monitor, 429, "")
    first = retailer.cooldown_until
    assert retailer.strikes == 1
    assert "blocked" in monitor.notifier.sent[-1]["title"]

    retailer.cooldown_until = 0  # cooldown over; blocked again
    sample(monitor, 429, "")
    assert retailer.strikes == 2
    assert retailer.cooldown_until - retailer.last_trip == pytest.approx(60 * 60)
    assert first


def test_restart_does_not_realert_while_still_in_stock(monitor, tmp_path):
    sample(monitor, 200, stock_json("inStock"))
    monitor._save()
    again = Monitor(monitor.options)
    again.notifier = FakeNotifier()
    sample(again, 200, stock_json("inStock"))
    again.log.close()
    assert again.notifier.sent == []


def events(m):
    path = m.dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_events_and_check_history_are_recorded(monitor):
    sample(monitor, 200, stock_json("outOfStock"))
    monitor._record_check(monitor.states["ps"])
    sample(monitor, 200, stock_json("inStock"))
    assert [e["kind"] for e in events(monitor)] == ["alert"]
    assert events(monitor)[0]["delivered"] is None  # no webhook configured
    rows = next(monitor.dir.glob("checks-*.csv")).read_text().splitlines()
    assert rows[0].startswith("ts,target") and ",ps,200,5,0," in rows[1]


def test_a_lone_timeout_is_a_failure_not_a_block(monitor):
    async def timeout():
        return FetchResult(None, 20000, "", "https://shop.example/p", timed_out=True, error="timeout")

    t = monitor.targets[0]
    asyncio.run(monitor._sample(t, monitor.states["ps"], timeout, PARSERS[t.parser]))
    assert monitor.retailers["PS Direct"].strikes == 0
    assert monitor.states["ps"].consecutive_errors == 1


def test_flapping_cross_check_is_not_a_change(monitor):
    from ps5probe.models import ParseResult
    from ps5probe.monitor import signal_key

    a = ParseResult(True, False, "jsonld=OutOfStock; atc='Select Condition' (ignored)", "GameStop")
    b = ParseResult(True, False, "jsonld=OutOfStock; atc='Not Available' [disabled]", "GameStop")
    c = ParseResult(True, False, "jsonld=PreOrder; atc='Not Available' [disabled]", "GameStop")
    assert signal_key(a) == signal_key(b) != signal_key(c)


def test_commands(monitor):
    st = monitor.states["ps"]
    monitor._command({"cmd": "pause", "target": "ps"})
    assert st.paused_until == PAUSED_FOREVER
    monitor._command({"cmd": "pause", "target": "ps", "minutes": 5})
    assert 290 < st.paused_until - time.time() <= 300
    monitor._command({"cmd": "resume", "target": "ps"})
    assert st.paused_until == 0
    monitor._command({"cmd": "check_now", "target": "ps"})
    assert "ps" in monitor._force

    r = monitor.retailers["PS Direct"]
    r.cooldown_until, r.strikes = time.time() + 600, 2
    monitor._command({"cmd": "clear_cooldown", "retailer": "PS Direct"})
    assert (r.cooldown_until, r.strikes) == (0, 0)
    assert [e["kind"] for e in events(monitor)] == ["command"] * 5
    with pytest.raises(KeyError):
        monitor._command({"cmd": "explode", "target": "ps"})


def test_zero_interval_turns_a_monitor_target_off():
    options = config.from_dict({
        "monitor": {"targets": {"a": 10, "b": 0}},
        "targets": [{"name": "a", "parser": "psdirect-api"}, {"name": "b", "parser": "psdirect-api"}],
    })
    assert options.monitor.targets == {"a": 10}
