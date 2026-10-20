"""Records passed between the fetchers, parsers, alert rule and log."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Challenge(StrEnum):
    NONE = "None"
    RATE_LIMITED = "RateLimited"
    FORBIDDEN = "Forbidden"
    PERIMETERX = "PerimeterX"
    CLOUDFLARE = "Cloudflare"
    AKAMAI = "Akamai"
    AMAZON_CAPTCHA = "AmazonCaptcha"
    AMAZON_CHECKPOINT = "AmazonCheckpoint"
    WALMART_BLOCK = "WalmartBlock"
    TIMEOUT = "Timeout"


@dataclass(frozen=True)
class FetchResult:
    status: int | None
    latency_ms: int
    body: str
    final_url: str
    timed_out: bool = False
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    bytes: int = 0  # roughly what came over the wire (browser: content-length of what loaded)
    proxy: str | None = None  # ip:port, when a proxy was used


@dataclass(frozen=True)
class ParseResult:
    signal_found: bool
    in_stock: bool | None = None
    raw_signal: str | None = None
    seller: str | None = None
    price: Decimal | None = None
    condition: str | None = None
    note: str | None = None

    @classmethod
    def not_found(cls, note: str) -> ParseResult:
        return cls(False, note=note)

    @property
    def fingerprint(self) -> str:
        """What we compare between samples to spot a change."""
        return f"{self.in_stock}|{self.raw_signal}|{self.seller}|{self.price}|{self.condition}"


@dataclass(frozen=True)
class AlertDecision:
    eligible: bool
    reason: str
