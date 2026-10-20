"""The goals-doc rule: in stock, sold by the retailer itself, at or under the max price, and New.

Phase 1 only logs the decision; nothing is sent.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .config import Target
from .models import AlertDecision, ParseResult

_NOT_NEW = re.compile(r"used|refurb|pre-?owned|open ?box|renewed|restored|damaged", re.IGNORECASE)


def evaluate(p: ParseResult, t: Target, max_price: Decimal) -> AlertDecision:
    if not p.signal_found:
        return AlertDecision(False, "no stock signal")
    if p.in_stock is not True:
        return AlertDecision(False, "not in stock")
    if p.seller is None:
        return AlertDecision(False, "seller unknown")
    if t.allowed_sellers and p.seller.casefold() not in {a.casefold() for a in t.allowed_sellers}:
        return AlertDecision(False, f"seller '{p.seller}' is not the retailer")
    if p.price is None:
        return AlertDecision(False, "price unknown")
    limit = t.max_price if t.max_price is not None else max_price
    if p.price > limit:
        return AlertDecision(False, f"price {p.price} > {limit}")
    if p.condition is not None and _NOT_NEW.search(p.condition):
        return AlertDecision(False, f"condition '{p.condition}'")
    return AlertDecision(True, "ALERT")
