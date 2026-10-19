"""Helpers the retailer parsers share: lenient JSON access, prices, HTML and schema.org."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from bs4 import BeautifulSoup, Tag

_PRICE = re.compile(r"\d+(\.\d{1,2})?")
_BUYABLE = {"InStock", "LimitedAvailability", "OnlineOnly", "PreOrder", "PreSale"}


# --- JSON ---------------------------------------------------------------------------------------

def parse_json(text: str) -> Any:
    """Parsed JSON with exact decimals, or None if it isn't JSON."""
    try:
        return json.loads(text, parse_float=Decimal)
    except (ValueError, TypeError, RecursionError):
        return None


def get(node: Any, *path: str) -> Any:
    """Walks object keys; None as soon as a step is missing, null or not an object."""
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def as_str(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    return None


def as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def as_price(v: Any) -> Decimal | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, Decimal)):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, str):
        return parse_price(v)
    return None


def items(v: Any) -> list[Any]:
    """An array's elements, or a lone object as a one-item list."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return []


def parse_price(text: str | None) -> Decimal | None:
    if text is None:
        return None
    m = _PRICE.search(text.replace(",", ""))
    return Decimal(m.group()) if m else None


# --- HTML ---------------------------------------------------------------------------------------

def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def text_of(doc: Tag, selector: str) -> str | None:
    el = doc.select_one(selector)
    return el.get_text().strip() if el is not None else None


def disabled(el: Tag) -> bool:
    return (
        el.has_attr("disabled")
        or (el.get("aria-disabled") or "").lower() == "true"
        or "disabled" in (el.get("class") or [])
    )


# --- schema.org ---------------------------------------------------------------------------------

def json_ld(doc: Tag) -> Iterator[dict[str, Any]]:
    """Every JSON-LD node on the page, with arrays and @graph flattened."""
    for script in doc.select("script[type='application/ld+json']"):
        root = parse_json(script.get_text())
        if root is not None:
            yield from _flatten(root)


def _flatten(e: Any) -> Iterator[dict[str, Any]]:
    if isinstance(e, list):
        for x in e:
            yield from _flatten(x)
    elif isinstance(e, dict):
        graph = e.get("@graph")
        if graph is not None:
            yield from _flatten(graph)
        else:
            yield e


def is_type(e: Any, type_: str) -> bool:
    t = get(e, "@type")
    return t == type_ or (isinstance(t, list) and type_ in t)


def offers(product: Any) -> Iterator[Any]:
    """Offers of a JSON-LD Product, unwrapping AggregateOffer."""
    for o in items(get(product, "offers")):
        if is_type(o, "AggregateOffer") and get(o, "offers") is not None:
            yield from items(o["offers"])
        else:
            yield o


def last_segment(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def buyable(availability: str | None) -> bool | None:
    """schema.org availability URL -> buyable now? PreOrder counts: it can be bought at retail."""
    if availability is None:
        return None
    return last_segment(availability) in _BUYABLE


def condition(item_condition: str | None) -> str | None:
    """schema.org itemCondition URL -> short label."""
    if item_condition is None:
        return None
    return last_segment(item_condition).replace("Condition", "") or None
