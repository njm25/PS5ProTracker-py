import asyncio

import pytest

from ps5probe import config
from ps5probe.config import Options, Ramp, Target
from ps5probe.log import ProbeLog
from ps5probe.models import FetchResult
from ps5probe.parsers import PARSERS
from ps5probe.runner import Runner

OUT_OF_STOCK_JSON = '{"products":[{"price":{"value":899},"stock":{"stockLevelStatus":"outOfStock"}}]}'


@pytest.fixture
def log(tmp_path):
    with ProbeLog(tmp_path) as log:
        yield log


def make_target(name, **ramp):
    ramp = {"intervals_seconds": [60], "samples_per_step": 3, "jitter": 0, **ramp}
    return Target(name=name, retailer="Shop", parser="psdirect-api", ramp=Ramp(**ramp))


def respond(status, body):
    async def fetch():
        return FetchResult(status, 5, body, "https://shop.example/p")

    return fetch


def test_challenge_trips_target_and_stops_sibling_on_same_retailer(log):
    async def scenario():
        options = Options()
        parse = PARSERS["psdirect-api"]
        blocked = Runner(make_target("a"), options, log, respond(429, "slow down"), parse)
        healthy = Runner(make_target("b"), options, log, respond(200, OUT_OF_STOCK_JSON), parse)
        blocked.on_trip = lambda _, reason: healthy.stop(f"a tripped ({reason})")

        healthy_run = asyncio.create_task(healthy.run())
        await asyncio.sleep(0.05)  # first sample, then it waits 60s
        a = await blocked.run()
        b = await asyncio.wait_for(healthy_run, 10)
        return a, b

    a, b = asyncio.run(scenario())
    assert a.samples == 1
    assert a.trip_reason == "RateLimited"
    assert a.tripped_at_interval == 60
    assert b.samples == 1
    assert b.trip_reason == "stopped: a tripped (RateLimited)"


def test_repeated_parse_failures_trip_after_the_limit(log):
    options = Options()
    runner = Runner(make_target("c", intervals_seconds=[0], samples_per_step=10), options, log,
                    respond(200, "{}"), PARSERS["psdirect-api"])

    s = asyncio.run(runner.run())

    assert s.samples == options.max_consecutive_errors
    assert s.trip_reason.startswith("3 consecutive failures")


def test_ramp_merges_default_then_target_then_profile():
    options = config.from_dict({
        "default_ramp": {"intervals_seconds": [120, 60], "samples_per_step": 10, "jitter": 0.2},
        "targets": [{"name": "x", "ramp": {"samples_per_step": 4}}],
        "profiles": {"fast": {"headless": False, "targets": {"X": {"ramp": {"intervals_seconds": [5]}, "settle_ms": 1}}}},
    })
    assert options.targets[0].ramp == Ramp([120, 60], 4, 0.2)

    config.apply_profile(options, "FAST")
    t = options.targets[0]
    assert t.ramp == Ramp([5], 4, 0.2)
    assert t.settle_ms == 1
    assert options.headless is False


def test_unknown_setting_is_a_config_error():
    with pytest.raises(config.ConfigError, match="targets.x: unknown setting"):
        config.from_dict({"targets": [{"name": "x", "Url": "https://example.com"}]})


def test_checked_in_config_loads():
    options = config.load()
    assert {t.parser for t in options.targets} <= set(PARSERS)
    for name in options.profiles:
        config.apply_profile(config.load(), name)
