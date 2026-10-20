import asyncio
import json
import time

import pytest

from ps5probe import config
from ps5probe.models import FetchResult
from ps5probe.monitor import Monitor
from ps5probe.parsers import PARSERS
from ps5probe.proxies import FAILS_TO_REST, Pool, Proxy, parse_list, read_cache, save_cache

URL = "https://proxy.example/list?token=secret"


def pool(n=3):
    return Pool([Proxy(f"10.0.0.{i}", 8000 + i, "u", "pw") for i in range(n)], 30, 60)


def test_parse_list_formats():
    ps = parse_list("# webshare\n1.2.3.4:5933:user:pass\n\n5.6.7.8:80\n"
                    "http://a:b@9.9.9.9:3128\n")
    assert [p.label for p in ps] == ["1.2.3.4:5933", "5.6.7.8:80", "9.9.9.9:3128"]
    assert ps[0].url == "http://user:pass@1.2.3.4:5933" and ps[1].url == "http://5.6.7.8:80"
    assert ps[0].playwright() == {"server": "http://1.2.3.4:5933", "username": "user", "password": "pass"}
    assert ps[0].redact("bad proxy http://user:pass@1.2.3.4") == "bad proxy http://user:***@1.2.3.4"
    with pytest.raises(ValueError):
        parse_list("not a proxy")


def test_cache_is_private_and_tied_to_its_url(tmp_path):
    path = tmp_path / "proxies.json"
    save_cache(path, URL, pool(2).proxies)
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.label for p in read_cache(path, URL)[0]] == ["10.0.0.0:8000", "10.0.0.1:8001"]
    assert read_cache(path, URL + "x") is None


def test_rotate_sticky_and_rest():
    p = pool()
    assert [p.pick("W", "t", sticky=False).label for _ in range(4)] == \
        ["10.0.0.0:8000", "10.0.0.1:8001", "10.0.0.2:8002", "10.0.0.0:8000"]

    first = p.pick("W", "s", sticky=True)
    assert p.pick("W", "s", sticky=True) == first
    assert p.block("W", first, "WalmartBlock") == 30
    assert p.pick("W", "s", sticky=True) != first  # moved on
    assert p.pick("Other", "o", sticky=True) is not None  # the rest is per retailer
    assert first in p.available("Other") and first not in p.available("W")
    assert p.block("W", first, "again") == 60  # doubles, capped at the max

    for q in p.proxies:
        p.block("W", q, "x")
    assert p.pick("W", "s", sticky=True) is None and p.next_free("W") > time.time()
    p.clear("W")
    assert len(p.available("W")) == 3


def test_failures_in_a_row_rest_a_proxy():
    p = pool(1)
    q = p.proxies[0]
    for i in range(FAILS_TO_REST - 1):
        assert not p.record("W", q, 100, ok=False, error="timeout")
    assert p.record("W", q, 100, ok=False, error="timeout")
    assert p.available("W") == [] and p.stats[q.label].blocks == 0 and p.stats[q.label].bytes_day == 300


@pytest.fixture
def proxied(tmp_path):
    options = config.from_dict({
        "output_dir": str(tmp_path),
        "monitor": {"targets": {"ps": 10}},
        "proxies": {"list_url": URL, "targets": {"ps": "rotate"}},
        "targets": [{"name": "ps", "retailer": "PS Direct", "parser": "psdirect-api", "url": "https://shop.example/p"}],
    })
    save_cache(tmp_path / ".monitor" / "proxies.json", URL, pool().proxies)
    m = Monitor(options)
    yield m
    m.log.close()


def test_a_block_through_a_proxy_rests_only_that_proxy(proxied):
    m = proxied
    t, st = m.targets[0], m.states["ps"]
    q = m.pool.pick("PS Direct", "ps", sticky=False)

    async def blocked():
        return FetchResult(429, 5, "", "https://shop.example/p", bytes=50)

    asyncio.run(m._sample(t, st, blocked, PARSERS[t.parser], q))
    assert m.cooldowns["PS Direct"].cooldown_until == 0 and m.cooldowns["ps"].cooldown_until == 0
    assert q not in m.pool.available("PS Direct") and len(m.pool.available("PS Direct")) == 2
    assert st.proxy == q.label
    m._save()
    saved = json.loads(m.state_path.read_text())["proxies"]
    assert saved["count"] == 3 and saved["modes"] == {"ps": "rotate"} and "pw" not in json.dumps(saved)
    assert saved["rests"]["PS Direct"][q.label]["strikes"] == 1


def test_bad_proxy_settings_are_rejected():
    base = {"targets": [{"name": "b", "method": "browser", "parser": "psdirect-api"}]}
    with pytest.raises(config.ConfigError, match="can't rotate"):
        config.from_dict({**base, "proxies": {"targets": {"b": "rotate"}}})
    with pytest.raises(config.ConfigError, match="one of"):
        config.from_dict({**base, "proxies": {"targets": {"b": "always"}}})
    assert config.from_dict({**base, "proxies": {"targets": {"b": "off"}}}).proxies.targets == {}


def test_past_the_daily_cap_proxied_targets_go_direct(proxied):
    m = proxied
    m.options.proxies.daily_mb = 1
    q = m.pool.proxies[0]
    m.pool.record("PS Direct", q, 2_000_000, ok=True)
    assert m.pool.mb_today() == 2.0
    m.notifier = type("N", (), {"sent": [], "send": lambda self, text, **k: _ok(self, text)})()
    assert asyncio.run(m._over_budget()) and asyncio.run(m._over_budget())
    assert len(m.notifier.sent) == 1  # posted once a day


async def _ok(n, text):
    n.sent.append(text)
    return True
