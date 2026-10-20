"""Classifies a response as a bot-protection challenge or block.

Several vendors inject their scripts into normal pages too, so marker strings only count
alongside an error status or a challenge-page title.
"""

from __future__ import annotations

import html
import re

from .models import Challenge, FetchResult

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def detect(r: FetchResult) -> Challenge:
    if r.timed_out:
        return Challenge.TIMEOUT

    # Akamai (Best Buy) resets the stream on clients it has classified as bots.
    if r.error and "ERR_HTTP2_PROTOCOL_ERROR" in r.error:
        return Challenge.AKAMAI

    body = r.body or ""
    lbody = body.lower()
    t = (title(body) or "").lower()
    url = (r.final_url or "").lower()
    error_status = r.status is not None and r.status >= 400 and r.status != 404

    if "/checkpoint/" in url or 'please click the "continue shopping" button' in lbody:
        return Challenge.AMAZON_CHECKPOINT

    if "validateCaptcha" in body or "make sure you're not a robot" in lbody:
        return Challenge.AMAZON_CAPTCHA

    # Walmart/Sam's: a redirect to /blocked, the "Robot or human?" page, Sam's "Let us know you're not
    # a robot" page (a 200 in a browser), or a 412 whose JSON body carries PerimeterX's redirectUrl/blockScript.
    if "/blocked?" in url or "robot or human" in t or "not a robot" in t \
            or (error_status and '"blockScript"' in body):
        return Challenge.WALMART_BLOCK

    if "px-captcha" in body and (error_status or "access to this page has been denied" in t):
        return Challenge.PERIMETERX

    if t.startswith("just a moment") or (
        r.status in (403, 429, 503) and any(m in body for m in ("challenge-platform", "cf-chl", "cf_chl"))
    ):
        return Challenge.CLOUDFLARE

    if r.status == 403 and "access denied" in lbody and ("Reference" in body or "edgesuite" in body):
        return Challenge.AKAMAI

    if r.status == 429:
        return Challenge.RATE_LIMITED
    if r.status in (403, 412, 435):
        return Challenge.FORBIDDEN
    return Challenge.NONE


def title(body: str) -> str | None:
    m = _TITLE.search(body)
    return html.unescape(m.group(1)).strip() if m else None
