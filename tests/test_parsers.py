"""Minimal fixtures shaped like what each site returned on 2026-10-15."""

from decimal import Decimal

import pytest

from ps5probe.alert import evaluate
from ps5probe.config import Target
from ps5probe.models import ParseResult
from ps5probe.parsers import PARSERS, safe_parse

MAX_PRICE = Decimal("899.99")


def target(**params):
    return Target(name="t", params=params)


def run(parser, body, t=None):
    return safe_parse(PARSERS[parser], body, t or target())


def eligible(r, *sellers):
    return evaluate(r, Target(allowed_sellers=list(sellers)), MAX_PRICE).eligible


BESTBUY_LD = """
<script type="application/ld+json">{"@type":"Product","sku":"6601524","offers":[{"@type":"Offer","price":899.99,
"availability":"https://schema.org/InStock","itemCondition":"https://schema.org/NewCondition","seller":{"name":"Best Buy"}}]}</script>
"""


def test_bestbuy_sold_out_button_wins_over_jsonld_and_carousel():
    html = f"""
        <html><head>{BESTBUY_LD}</head><body>
        <button data-testid="pdp-sold-out-6601524" disabled>Sold Out</button>
        <button data-testid="carousel-add-to-cart-6503672">Add to cart</button>
        </body></html>
        """
    r = run("bestbuy-page", html, target(sku="6601524"))
    assert r.signal_found
    assert r.in_stock is False
    assert r.raw_signal == "pdp-sold-out-6601524 [disabled]"
    assert r.seller == "Best Buy"
    assert r.price == Decimal("899.99")
    assert r.condition == "New"


def test_bestbuy_add_to_cart_for_the_sku_is_in_stock():
    html = f'<html><head>{BESTBUY_LD}</head><body><button data-testid="pdp-add-to-cart-6601524">Add to cart</button></body></html>'
    r = run("bestbuy-page", html, target(sku="6601524"))
    assert r.in_stock is True
    assert eligible(r, "Best Buy")


def test_bestbuy_carousel_only_is_not_a_signal():
    r = run("bestbuy-page", '<button data-testid="carousel-add-to-cart-123">Add to cart</button>', target(sku="6601524"))
    assert not r.signal_found


def next_data(product):
    return (
        '<html><body><h1>Uh-oh...</h1><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"initialData":{"data":{"product":' + product + "}}}}}</script></body></html>"
    )


def test_walmart_out_of_stock_page_still_parses():
    r = run("walmart-nextdata", next_data('{"availabilityStatus":"OUT_OF_STOCK","name":"","sellerName":null}'))
    assert r.signal_found
    assert r.in_stock is False
    assert r.raw_signal == "availabilityStatus=OUT_OF_STOCK"


def test_walmart_marketplace_seller_is_rejected_by_alert_rule():
    r = run("walmart-nextdata", next_data(
        '{"availabilityStatus":"IN_STOCK","sellerName":"HyperTech","sellerType":"EXTERNAL",'
        '"priceInfo":{"currentPrice":{"price":1644.99}}}'))
    assert r.in_stock is True
    d = evaluate(r, Target(allowed_sellers=["Walmart.com", "INTERNAL"]), MAX_PRICE)
    assert not d.eligible
    assert "HyperTech" in d.reason


def test_walmart_redirect_to_marketplace_item_is_not_in_stock():
    r = run(
        "walmart-nextdata",
        next_data('{"usItemId":"18235967161","availabilityStatus":"IN_STOCK","sellerName":"HyperTech",'
                  '"priceInfo":{"currentPrice":{"price":1644.99}}}'),
        target(item_id="11839818124"),
    )
    assert r.signal_found
    assert r.in_stock is False
    assert r.raw_signal == "redirected to usItemId=18235967161"


def test_samsclub_internal_seller_in_stock_alerts():
    r = run("walmart-nextdata", next_data(
        '{"availabilityStatus":"IN_STOCK","sellerName":null,"sellerType":"INTERNAL",'
        '"priceInfo":{"currentPrice":{"price":899}}}'))
    assert r.seller == "INTERNAL"
    assert eligible(r, "Sam's Club", "INTERNAL")


@pytest.mark.parametrize(("status", "expected"), [("outOfStock", False), ("lowStock", True), ("inStock", True)])
def test_psdirect_uses_stock_level_status_not_purchasable(status, expected):
    body = (
        '{"products":[{"code":"1000050928","purchasable":true,"maxOrderQuantity":0,"price":{"value":899},'
        '"stock":{"stockLevelStatus":"' + status + '"}}]}'
    )
    r = run("psdirect-api", body)
    assert r.in_stock is expected
    assert r.price == Decimal(899)


def test_psdirect_page_microdata_offer():
    html = """
        <div itemscope itemtype="http://schema.org/Product"><meta itemprop="price" content="899.00"/>
        <div itemscope itemtype="http://schema.org/Offer"><link itemprop="availability" href="https://schema.org/OutOfStock"/></div></div>
        """
    r = run("schema-offer", html, target(default_seller="PlayStation Direct"))
    assert r.signal_found
    assert r.in_stock is False
    assert r.raw_signal == "microdata=OutOfStock"
    assert r.seller == "PlayStation Direct"
    assert r.price == Decimal("899.00")


def test_gamestop_picks_new_offer_and_checks_button():
    html = """
        <script type="application/ld+json">[{"@type":"Product","offers":[
          {"@type":"Offer","name":"Pre-Owned","sku":"416189","price":"1329.99","availability":"https://schema.org/InStock","itemCondition":"https://schema.org/UsedCondition","seller":{"name":"GameStop"}},
          {"@type":"Offer","name":"New","sku":"416188","price":"899.99","availability":"https://schema.org/OutOfStock","itemCondition":"https://schema.org/NewCondition","seller":{"name":"GameStop"}}]}]</script>
        <button class="js-add-to-cart add-to-cart btn disabled" data-pid="20015604" disabled>Not Available</button>
        """
    r = run("gamestop-page", html, target(sku="416188", pid="20015604"))
    assert r.in_stock is False
    assert r.price == Decimal("899.99")
    assert r.condition == "New"
    assert "atc='Not Available' [disabled]" in r.raw_signal


def test_gamestop_select_condition_button_does_not_mask_new_in_stock():
    html = """
        <script type="application/ld+json">{"@type":"Product","offers":[
          {"@type":"Offer","name":"New","sku":"416188","price":"899.99","availability":"https://schema.org/InStock","itemCondition":"https://schema.org/NewCondition","seller":{"name":"GameStop"}}]}</script>
        <button class="js-add-to-cart add-to-cart btn disabled" data-pid="20015604" disabled>Select Condition</button>
        """
    r = run("gamestop-page", html, target(sku="416188", pid="20015604"))
    assert r.in_stock is True
    assert "(ignored)" in r.raw_signal
    assert eligible(r, "GameStop")


def test_gamestop_used_offer_in_stock_does_not_alert_when_filtered_to_new_sku():
    html = """
        <script type="application/ld+json">{"@type":"Product","offers":[
          {"@type":"Offer","sku":"416189","price":"1329.99","availability":"https://schema.org/InStock","itemCondition":"https://schema.org/UsedCondition","seller":{"name":"GameStop"}}]}</script>
        <button class="add-to-cart" data-pid="20015604">Add to Cart</button>
        """
    r = run("gamestop-page", html, target(sku="416188", pid="20015604"))
    assert not eligible(r, "GameStop")


def test_newegg_marketplace_buybox_is_not_newegg():
    html = """
        <script>window.__initialState__ = {"ItemDetail":{"Instock":true,"FinalPrice":1599,"SellerType":"D",
          "RankInfo":{"ItemNumber":"9SIAN2HKUK0984"},"Seller":{"SellerName":"Ozeal Electronics"},
          "Description":{"Title":"PlayStation 5 Pro Console"}}};</script>
        """
    r = run("newegg-page", html)
    assert r.in_stock is True
    assert r.seller == "Ozeal Electronics"
    assert not eligible(r, "Newegg")


def test_newegg_first_party_item():
    html = ('<script>window.__initialState__ = {"ItemDetail":{"Instock":true,"FinalPrice":899.99,'
            '"ItemNumber":"N82E16868110346","Seller":null}};</script>')
    r = run("newegg-page", html)
    assert r.seller == "Newegg"
    assert r.condition == "New"
    assert eligible(r, "Newegg")


def test_target_empty_buybox_reads_as_out_of_stock():
    r = run("target-page",
            '<html><body><h1>PlayStation 5 Pro Console</h1><script>{\\"relationship_type_code\\":\\"SA\\"}</script></body></html>')
    assert r.signal_found
    assert r.in_stock is False
    assert r.seller == "Target"


def test_target_enabled_ship_button_is_in_stock():
    r = run("target-page", '<div data-test="product-price">$899.99</div><button data-test="shipItButton">Ship it</button>'
                           "<p>Sold and shipped by Target</p>")
    assert r.in_stock is True
    assert r.price == Decimal("899.99")
    assert r.seller == "Target"


def test_amazon_buybox_sold_by_amazon():
    html = """
        <div id="corePrice_feature_div"><span class="a-offscreen">$899.99</span></div>
        <div id="availability"> In Stock </div>
        <div id="merchantInfoFeature_feature_div"><span class="offer-display-feature-text-message">Amazon.com</span></div>
        <input id="add-to-cart-button" type="submit">
        """
    r = run("amazon-page", html)
    assert r.in_stock is True
    assert r.seller == "Amazon.com"
    assert eligible(r, "Amazon.com", "Amazon")


def test_price_above_max_does_not_alert():
    p = ParseResult(True, True, "x", "Best Buy", Decimal("900.00"), "New")
    assert not eligible(p, "Best Buy")


def test_parser_crash_becomes_not_found():
    r = run("bestbuy-page", "<html></html>", target())  # missing sku param raises inside
    assert not r.signal_found
    assert r.note.startswith("parser error")


def lenovo_body(num, status=2):
    return ('{"success":true,"status":200,"data":[{"productCode":"78814903","status":%d,"currentNum":%d}]}'
            % (status, num))


@pytest.mark.parametrize("num, in_stock", [(0, False), (3, True)])
def test_lenovo_inventory(num, in_stock):
    t = Target(name="l", parser="lenovo-inventory", params={"product_code": "78814903", "list_price": "899.99"},
               allowed_sellers=["Lenovo"])
    r = run("lenovo-inventory", lenovo_body(num), t)
    assert (r.signal_found, r.in_stock, r.seller, r.price) == (True, in_stock, "Lenovo", Decimal("899.99"))
    assert evaluate(r, t, MAX_PRICE).eligible is in_stock
    assert not run("lenovo-inventory", '{"success":false,"msg":"bad"}', t).signal_found


def verizon_body(in_stock):
    v = "true" if in_stock else "false"
    flight = ('"inventoryDetails":{"dFillInventory":{"isAvailable":%s,"isBackorder":false,"isInStock":%s,'
              '"qtyAvailable":%d}},"storages":[{"isInStock":%s,"price":699.99,"sorId":"1000046529","skuId":"sku1"}]'
              % (v, v, 5 if in_stock else 0, v))
    return '<script>self.__next_f.push([1,"%s"])</script>' % flight.replace('"', '\\"')


@pytest.mark.parametrize("in_stock", [False, True])
def test_verizon_page(in_stock):
    t = Target(name="v", parser="verizon-page", params={"sku": "1000046529"}, allowed_sellers=["Verizon"])
    r = run("verizon-page", verizon_body(in_stock), t)
    assert (r.signal_found, r.in_stock, r.price) == (True, in_stock, Decimal("699.99"))
    assert evaluate(r, t, MAX_PRICE).eligible is in_stock
    assert not run("verizon-page", "<html></html>", t).signal_found


def test_a_target_can_raise_its_own_max_price():
    r = ParseResult(True, True, "jsonld=InStock", "Staples", Decimal("906.99"), "New")
    assert not evaluate(r, Target(allowed_sellers=["Staples"]), MAX_PRICE).eligible
    assert evaluate(r, Target(allowed_sellers=["Staples"], max_price=Decimal("909.99")), MAX_PRICE).eligible
