from ps5probe.challenge import detect
from ps5probe.models import Challenge, FetchResult


def r(status, body, url="https://example.com/p", timed_out=False):
    return FetchResult(status, 100, body, url, timed_out)


def test_target_perimeterx_435():
    body = '<title>Access to this page has been denied</title><meta name="description" content="px-captcha">'
    assert detect(r(435, body)) is Challenge.PERIMETERX


def test_perimeterx_script_on_a_normal_page_is_not_a_challenge():
    assert detect(r(200, "<title>PS5 Pro</title><script>window._pxAppId='x'; // px-captcha</script>")) is Challenge.NONE


def test_cloudflare_403_challenge():
    body = '<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/h/b"></script>'
    assert detect(r(403, body)) is Challenge.CLOUDFLARE


def test_cloudflare_beacon_on_a_200_page_is_fine():
    body = '<title>GameStop</title><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    assert detect(r(200, body)) is Challenge.NONE


def test_amazon_checkpoint_by_url():
    assert detect(r(200, "<html></html>", "https://www.amazon.com/checkpoint/continue?token=x")) is Challenge.AMAZON_CHECKPOINT


def test_walmart_404_with_data_is_not_a_challenge():
    assert detect(r(404, '<script id="__NEXT_DATA__">{}</script>')) is Challenge.NONE


def test_walmart_412_perimeterx_json():
    body = '{"redirectUrl":"/blocked?url=Lw==","appId":"PXu6b0qd2S","blockScript":"/px/PXu6b0qd2S/captcha/captcha.js"}'
    assert detect(r(412, body)) is Challenge.WALMART_BLOCK


def test_bestbuy_http2_reset_is_akamai():
    f = FetchResult(None, 100, "", "https://www.bestbuy.com/x", False,
                    "net::ERR_HTTP2_PROTOCOL_ERROR at https://www.bestbuy.com/x")
    assert detect(f) is Challenge.AKAMAI


def test_timeout_counts_as_block():
    assert detect(r(None, "", timed_out=True)) is Challenge.TIMEOUT


def test_status_429_is_rate_limited():
    assert detect(r(429, "slow down")) is Challenge.RATE_LIMITED


def test_samsclub_robot_page_in_a_browser():
    body = "<html><head><title>Let us know you're not a robot - Sam's Club</title></head>" \
           '<script id="blockScript">px-captcha</script></html>'
    assert detect(r(200, body)) is Challenge.WALMART_BLOCK
