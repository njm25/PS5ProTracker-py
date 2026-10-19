"""Discord webhook posts. Without a webhook URL, messages only go to the console."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import Discord
from .log import say

GREEN, RED, AMBER, GREY = 0x2ECC71, 0xE74C3C, 0xF1C40F, 0x95A5A6


class Notifier:
    def __init__(self, discord: Discord):
        self._discord = discord

    def send_sync(self, text: str, *, ping: bool = False, title: str | None = None,
                  url: str | None = None, color: int | None = None, fields: dict[str, Any] | None = None) -> bool:
        """Posts one message; True if Discord took it (or there's no webhook and it was printed)."""
        say("notify", text if title is None else f"{title}: {text}")
        if not self._discord.webhook_url:
            return True
        embed = {
            "title": title,
            "description": text,
            "url": url,
            "color": color,
            "fields": [{"name": k, "value": str(v), "inline": True} for k, v in (fields or {}).items()],
        }
        payload: dict[str, Any] = {
            # Only alerts may ping; everything else is silent even if the text contains @here.
            "allowed_mentions": {"parse": ["everyone", "roles", "users"] if ping else []},
            "embeds": [{k: v for k, v in embed.items() if v not in (None, [])}],
        }
        if ping:
            payload["content"] = f"{self._discord.mention} {title or ''}".strip()
        for _ in range(3):
            try:
                r = httpx.post(self._discord.webhook_url, json=payload, timeout=15)
            except httpx.HTTPError as ex:
                say("notify", f"Discord post failed: {ex}")
                time.sleep(2)
                continue
            if r.status_code == 429:  # rate limited: Discord says how long to wait
                time.sleep(min(float(r.json().get("retry_after", 2)), 30))
                continue
            if r.is_success:
                return True
            say("notify", f"Discord post failed: HTTP {r.status_code} {r.text[:200]}")
            return False
        return False

    async def send(self, text: str, **kwargs: Any) -> bool:
        return await asyncio.to_thread(self.send_sync, text, **kwargs)
