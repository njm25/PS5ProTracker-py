"""One stock parser per retailer page or API, registered by the name targets use in config.json."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace

from bs4 import BeautifulSoup

from .config import Target
from .models import ParseResult
from .parsing import (
    as_bool,
    as_price,
    as_str,
    buyable,
    condition,
    disabled,
    get,
    is_type,
    items,
    json_ld,
    last_segment,
    offers,
    parse_json,
    parse_price,
    soup,
    text_of,
)

Parser = Callable[[str, Target], ParseResult]
PARSERS: dict[str, Parser] = {}


def parser(name: str) -> Callable[[Parser], Parser]:
    def register(fn: Parser) -> Parser:
        PARSERS[name] = fn
        return fn

    return register


def safe_parse(parse: Parser, body: str, target: Target) -> ParseResult:
    """Runs a parser, turning a crash on unexpected markup into a "signal not found" row."""
    if not body:
        return ParseResult.not_found("empty body")
    try:
        return parse(body, target)
    except Exception as ex:
        return ParseResult.not_found(f"parser error: {type(ex).__name__}: {ex}")


def _join(*notes: str | None) -> str | None:
    return "; ".join(n for n in notes if n) or None


@parser("bestbuy-page")
def bestbuy_page(body: str, t: Target) -> ParseResult:
    """Best Buy product page.

    The JSON-LD reported InStock while the page said Sold Out (2026-10-15), so stock comes only
    from the SKU-specific buy button (data-testid="pdp-...-{sku}"). Carousel "Add to cart"
    buttons for other SKUs are ignored.
    """
    sku = t.param("sku")
    doc = soup(body)

    offer = next(
        (o for p in json_ld(doc) if is_type(p, "Product") and as_str(get(p, "sku")) == sku for o in offers(p)),
        None,
    )
    seller = as_str(get(offer, "seller", "name"))
    price = as_price(get(offer, "price"))
    cond = condition(as_str(get(offer, "itemCondition")))
    availability = as_str(get(offer, "availability"))
    ld_note = f"jsonld={last_segment(availability)} (ignored)" if availability is not None else None

    buttons = [
        b for b in doc.select("button[data-testid]")
        if b["data-testid"].startswith("pdp-") and b["data-testid"].endswith(f"-{sku}")
    ]
    if not buttons:
        return ParseResult(False, None, None, seller, price, cond, f"no pdp-*-{sku} button; {ld_note or ''}")

    primary = next((b for b in buttons if "add-to-cart" in b["data-testid"]), buttons[0])
    test_id = primary["data-testid"]
    is_disabled = disabled(primary)
    return ParseResult(
        True, "add-to-cart" in test_id and not is_disabled, test_id + (" [disabled]" if is_disabled else ""),
        seller, price, cond, ld_note,
    )


@parser("walmart-nextdata")
def walmart_nextdata(body: str, t: Target) -> ParseResult:
    """Walmart and Sam's Club product pages (same Next.js platform): props.pageProps.initialData.data.product.

    Walmart answers 404 with the data still present when it has no offer, so status is not checked here.
    """
    script = soup(body).select_one("script#__NEXT_DATA__")
    if script is None:
        return ParseResult.not_found("no __NEXT_DATA__")
    product = get(parse_json(script.get_text()), "props", "pageProps", "initialData", "data", "product")
    if product is None:
        return ParseResult.not_found("no data.product")
    status = as_str(get(product, "availabilityStatus"))
    if status is None:
        return ParseResult.not_found("no availabilityStatus")
    price = as_price(get(product, "priceInfo", "currentPrice", "price"))

    # With no first-party offer, Walmart redirected the item to a marketplace listing (2026-10-15).
    item_id = as_str(get(product, "usItemId"))
    expected = t.params.get("item_id")
    if expected is not None and item_id is not None and item_id != expected:
        return ParseResult(
            True, False, f"redirected to usItemId={item_id}", as_str(get(product, "sellerName")),
            price, None, f"expected {expected}",
        )

    seller_type = as_str(get(product, "sellerType"))
    seller = as_str(get(product, "sellerName")) or as_str(get(product, "sellerDisplayName")) or seller_type
    cond = as_str(get(product, "conditionType")) or ("Pre-owned" if as_bool(get(product, "isPreowned")) else None)
    return ParseResult(
        True, status == "IN_STOCK", f"availabilityStatus={status}", seller, price, cond,
        f"sellerType={seller_type}; showAtc={as_str(get(product, 'showAtc'))}",
    )





@parser("psdirect-api")
def psdirect_api(body: str, t: Target) -> ParseResult:
    """PlayStation Direct productList API.

    stock.stockLevelStatus is the signal; "purchasable" stays true while out of stock, so it is only logged.
    """
    product = next(iter(items(get(parse_json(body), "products"))), None)
    status = as_str(get(product, "stock", "stockLevelStatus"))
    if status is None:
        return ParseResult.not_found("no stock.stockLevelStatus")
    return ParseResult(
        True, status in ("inStock", "lowStock"),
        f"stockLevelStatus={status}; maxOrderQuantity={as_str(get(product, 'maxOrderQuantity'))}; "
        f"purchasable={as_str(get(product, 'purchasable'))}",
        "PlayStation Direct", as_price(get(product, "price", "value")), "New",
    )


@parser("schema-offer")
def schema_offer(body: str, t: Target) -> ParseResult:
    """Generic schema.org Offer reader: JSON-LD first, then microdata.

    PS Direct's server-rendered page uses itemprop="availability". Params: sku (optional, matches
    offer or product sku), default_seller (used when the offer names none). Prefers a New offer.
    """
    return _schema_offer(soup(body), t)


def _schema_offer(doc: BeautifulSoup, t: Target) -> ParseResult:
    sku = t.params.get("sku")
    found = [
        o for p in json_ld(doc) if is_type(p, "Product") for o in offers(p)
        if sku is None or as_str(get(o, "sku")) == sku or as_str(get(p, "sku")) == sku
    ]
    if not found:
        return _microdata(doc, t) if sku is None else ParseResult.not_found(f"no JSON-LD offer for sku {sku}")

    offer = next((o for o in found if condition(as_str(get(o, "itemCondition"))) == "New"), found[0])
    availability = as_str(get(offer, "availability"))
    if availability is None:
        return ParseResult.not_found("offer has no availability")
    return ParseResult(
        True, buyable(availability), f"jsonld={last_segment(availability)}",
        as_str(get(offer, "seller", "name")) or t.params.get("default_seller"),
        as_price(get(offer, "price")), condition(as_str(get(offer, "itemCondition"))),
        f"{len(found)} offers on page" if len(found) > 1 else None,
    )


def _microdata(doc: BeautifulSoup, t: Target) -> ParseResult:
    def prop(scope, name: str) -> str | None:
        e = scope.select_one(f"[itemprop='{name}']")
        return None if e is None else e.get("href") or e.get("content") or e.get_text().strip()

    found = doc.select("[itemtype*='schema.org/Offer']")
    offer = next((o for o in found if prop(o, "availability") is not None), None)
    if offer is None:
        return ParseResult.not_found("no JSON-LD or microdata offer")
    availability = prop(offer, "availability")
    return ParseResult(
        True, buyable(availability), f"microdata={last_segment(availability)}",
        prop(offer, "seller") or t.params.get("default_seller"),
        parse_price(prop(offer, "price") or prop(doc, "price")),
        condition(prop(offer, "itemCondition")) or "New",
        f"{len(found)} microdata offers" if len(found) > 1 else None,
    )


@parser("lenovo-inventory")
def lenovo_inventory(body: str, t: Target) -> ParseResult:
    """Lenovo's inventory API (POST openapi.lenovo.com/us/en/inventory/front/query): data[].currentNum.

    The product page's JSON-LD says InStock while the page shows "Available Soon" (it's an edge-cached
    copy), so this is the signal to trust. status 2 / currentNum 0 is "Available Soon" (10-20); other
    status values weren't seen yet, so any change shows up as a signal change. The API has no price:
    params.list_price stands in.
    """
    root = parse_json(body)
    if get(root, "success") is False:
        return ParseResult.not_found(f"api error: {as_str(get(root, 'msg'))}")
    code = t.param("product_code")
    item = next((i for i in items(get(root, "data")) if as_str(get(i, "productCode")) == code), None)
    if item is None:
        return ParseResult.not_found(f"no product {code}")
    num, status = get(item, "currentNum"), as_str(get(item, "status"))
    if not isinstance(num, int):
        return ParseResult.not_found("no currentNum")
    return ParseResult(True, num > 0, f"status={status}; currentNum={num}", "Lenovo",
                       as_price(t.params.get("list_price")), "New")


_VERIZON_INVENTORY = re.compile(r'"dFillInventory":(\{[^{}]*\})')


@parser("verizon-page")
def verizon_page(body: str, t: Target) -> ParseResult:
    """Verizon product page: the Next.js flight payload (escaped JSON in self.__next_f.push) carries
    dFillInventory {isAvailable, isInStock, qtyAvailable} and, per SKU, {isInStock, price, sorId}.
    Params: sku (the sorId, Sony's model number).
    """
    text = body.replace('\\"', '"')
    inv = _VERIZON_INVENTORY.search(text)
    sku = re.search(r'"isInStock":(true|false),"price":([\d.]+),"sorId":"%s"' % re.escape(t.param("sku")), text)
    if inv is None and sku is None:
        return ParseResult.not_found("no dFillInventory or sku entry")
    d = parse_json(inv.group(1)) if inv else {}
    shipping = bool(get(d, "isInStock") or get(d, "isAvailable"))
    listed = sku is not None and sku.group(1) == "true"
    return ParseResult(
        True, shipping or listed,
        f"isInStock={get(d, 'isInStock')}; isAvailable={get(d, 'isAvailable')}; qty={get(d, 'qtyAvailable')}; "
        f"sku={sku.group(1) if sku else None}",
        "Verizon", as_price(sku.group(2)) if sku else None, "New",
    )


@parser("gamestop-page")
def gamestop_page(body: str, t: Target) -> ParseResult:
    """GameStop: one page holds New, Pre-Owned and Refurbished.

    Uses the New JSON-LD offer (params.sku = the New variant SKU) and cross-checks the
    add-to-cart button for params.pid.
    """
    doc = soup(body)
    ld = _schema_offer(doc, t)
    pid = t.param("pid")
    atc = doc.select_one(f"button.add-to-cart[data-pid='{pid}']")
    if atc is None:
        return replace(ld, note=_join(ld.note, "no add-to-cart button"))

    atc_enabled = not disabled(atc)
    atc_text = atc.get_text().strip()
    atc_raw = f"atc='{atc_text}'" + ("" if atc_enabled else " [disabled]")

    # With no condition picked the button reads "Select Condition" and is disabled whatever the
    # stock (28 of 30 headed loads on 2026-10-16), so it says nothing about New.
    if "select condition" in atc_text.lower():
        return replace(
            ld,
            raw_signal=f"{ld.raw_signal}; atc='{atc_text}' (ignored)" if ld.signal_found else None,
            note=_join(ld.note, "button needs a condition selected; JSON-LD only"),
        )

    if not ld.signal_found:
        return ParseResult(True, atc_enabled, atc_raw, note=_join(ld.note, "stock from button only"))

    return replace(
        ld,
        in_stock=ld.in_stock is True and atc_enabled,
        raw_signal=f"{ld.raw_signal}; {atc_raw}",
        note=_join(ld.note, None if ld.in_stock == atc_enabled else "JSON-LD and button disagree"),
    )


_TARGET_BUY_BUTTONS = {"shipItButton", "orderPickupButton", "scheduledDeliveryButton", "addToCartButton"}
_TARGET_RELATIONSHIP = re.compile(r'relationship_type_code\\?"\s*:\s*\\?"(\w+)')
_TARGET_SOLD_BY = re.compile(r"Sold (?:and shipped )?by ([^\n]{2,40}?)(?:\n|$)")
_TARGET_OUT_OF_STOCK = re.compile(
    r"(Temporarily out of stock|Out of stock|Sold out|Notify me when it's back)", re.IGNORECASE
)


@parser("target-page")
def target_page(body: str, t: Target) -> ParseResult:
    """Target (rendered DOM).

    On 2026-10-15 an out-of-stock page showed no price and no buy buttons at all, so "nothing
    rendered" is read as out of stock (low confidence until seen in stock).
    """
    doc = soup(body)
    price = parse_price(text_of(doc, "[data-test='product-price']"))
    text = doc.body.get_text() if doc.body else ""
    if m := _TARGET_SOLD_BY.search(text):
        seller = m.group(1).strip()
    elif rel := _TARGET_RELATIONSHIP.search(body):
        seller = "Target" if rel.group(1) == "SA" else f"Target Plus ({rel.group(1)})"
    else:
        seller = None

    buttons = [b for b in doc.select("button[data-test]") if b["data-test"] in _TARGET_BUY_BUTTONS]
    if buttons:
        raw = ",".join(b["data-test"] + ("[disabled]" if disabled(b) else "") for b in buttons)
        return ParseResult(True, any(not disabled(b) for b in buttons), raw, seller, price)

    if oos := _TARGET_OUT_OF_STOCK.search(text):
        return ParseResult(True, False, f"text='{oos.group()}'", seller, price)

    if price is None:
        return ParseResult(True, False, "no buy box (no price, no buttons)", seller, note="low confidence")
    return ParseResult(False, seller=seller, price=price, note="price shown but no known buy buttons")


_NEWEGG_STATE = re.compile(r"window\.__initialState__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL)
_NEWEGG_NOT_NEW = re.compile(r"refurbished|renewed|open box|used", re.IGNORECASE)


@parser("newegg-page")
def newegg_page(body: str, t: Target) -> ParseResult:
    """Newegg: window.__initialState__.ItemDetail.

    The buy box is often a marketplace seller on the same URL, so the seller is "Newegg" only
    for an N82E item number with no third-party seller.
    """
    m = _NEWEGG_STATE.search(body)
    if not m:
        return ParseResult.not_found("no window.__initialState__")
    item = get(parse_json(m.group(1)), "ItemDetail")
    instock = as_bool(get(item, "Instock"))
    if instock is None:
        return ParseResult.not_found("no ItemDetail.Instock")

    item_number = (
        as_str(get(item, "ItemNumber"))
        or as_str(get(item, "RankInfo", "ItemNumber"))
        or as_str(get(item, "Item", "ItemNumber"))
        or ""
    )
    seller_name = as_str(get(item, "Seller", "SellerName"))
    if item_number.startswith("N82E") and (seller_name is None or "newegg" in seller_name.lower()):
        seller = "Newegg"
    else:
        seller = seller_name or f"unknown ({item_number})"
    title = as_str(get(item, "Description", "Title")) or ""
    if item_number.endswith("R"):
        cond = "Open Box"
    elif c := _NEWEGG_NOT_NEW.search(title):
        cond = c.group()
    else:
        cond = "New"
    return ParseResult(
        True, instock, f"Instock={instock}; ItemNumber={item_number}; SellerType={as_str(get(item, 'SellerType'))}",
        seller, as_price(get(item, "FinalPrice")), cond,
    )


_AMAZON_SOLD_BY = re.compile(r"Sold by\s+(\S[^\n]{1,40})")
_AMAZON_UNAVAILABLE = re.compile(r"unavailable|out of stock|invitation", re.IGNORECASE)


@parser("amazon-page")
def amazon_page(body: str, t: Target) -> ParseResult:
    """Amazon product page buy box. Disabled by default: a checkpoint page appeared on first load."""
    doc = soup(body)
    availability = text_of(doc, "#availability")
    atc = doc.select_one("#add-to-cart-button")
    if availability is None and atc is None:
        return ParseResult.not_found("no #availability or #add-to-cart-button")

    price = parse_price(
        text_of(doc, "#corePrice_feature_div .a-offscreen")
        or text_of(doc, "#corePriceDisplay_desktop_feature_div .a-offscreen")
    )
    sold_by = _AMAZON_SOLD_BY.search(text_of(doc, "#tabular-buybox") or "")
    seller = (
        text_of(doc, "#merchantInfoFeature_feature_div .offer-display-feature-text-message")
        or text_of(doc, "#sellerProfileTriggerId")
        or (sold_by.group(1).strip() if sold_by else None)
    )
    in_stock = atc is not None and (availability is None or not _AMAZON_UNAVAILABLE.search(availability))
    return ParseResult(
        True, in_stock, f"atc={'absent' if atc is None else 'present'}; availability='{availability}'",
        seller, price, "New" if doc.select_one("#usedOnlyBuybox") is None else "Used",
    )
