import asyncio

import pytest

from ps5probe import config
from ps5probe.models import FetchResult
from ps5probe.monitor import Monitor
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
