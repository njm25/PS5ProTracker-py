import asyncio

from ps5probe import config
from ps5probe.fetchers import BrowserFetcher, BrowserHost


class FrozenHost(BrowserHost):
    """A Chromium that stopped answering: opening a tab never returns."""

    def __init__(self, options):
        super().__init__(options)
        self.relaunched = 0

    async def new_context(self, proxy=None):
        await asyncio.Event().wait()

    async def relaunch(self):
        self.relaunched += 1


def test_a_frozen_browser_times_out_instead_of_hanging():
    options = config.from_dict({"request_timeout_seconds": 0.05,
                                "targets": [{"name": "b", "method": "browser", "parser": "psdirect-api", "settle_ms": 0}]})
    host = FrozenHost(options)
    f = BrowserFetcher(host, options)
    f.DEADLINE_SLACK = 0

    r = asyncio.run(asyncio.wait_for(f.fetch(options.targets[0]), 5))
    assert r.timed_out and r.error == "browser stopped responding" and host.relaunched == 1
