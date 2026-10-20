"""Changes the UI makes: monitor commands (via its inbox) and settings (via config.local.json)."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .. import config
from ..challenge import detect
from ..cli import build_fetch
from ..models import Challenge
from ..parsers import PARSERS, safe_parse
from .data import monitor_dir

COMMANDS = ("check_now", "pause", "resume", "clear_cooldown")
MIN_INTERVAL = 5
SECRET_MASK = "••••••••"  # stands in for a saved secret in the form; sent back unchanged, it means "keep it"
API_METHODS = ("bestbuy_api", "walmart_affiliate_api")


def command(cmd: str, **args: Any) -> None:
    """Queues a command; the monitor picks it up within about a second."""
    if cmd not in COMMANDS:
        raise ValueError(f"unknown command {cmd}")
    inbox = monitor_dir() / "commands"
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{time.time():.3f}-{uuid.uuid4().hex[:6]}"
    tmp = inbox / f"{name}.tmp"
    tmp.write_text(json.dumps({"cmd": cmd, **args}), encoding="utf-8")
    tmp.rename(inbox / f"{name}.json")  # the monitor only reads *.json, so it never sees half a file


# --- settings ---

LOCAL = config.ROOT / "config.local.json"
WALMART_KEY = config.ROOT / "keys" / "walmart.pem"


def _local() -> dict[str, Any]:
    try:
        return json.loads(LOCAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(form: dict[str, Any], targets: list[str]) -> list[str]:
    """Validates the settings form and writes it into config.local.json. Returns errors (none = saved)."""
    errors: list[str] = []
    local = _local()

    def number(key: str, kind: type, lo: float, label: str) -> Any:
        raw = (form.get(key) or "").strip()
        try:
            value = kind(raw)
        except (ValueError, InvalidOperation):
            errors.append(f"{label}: '{raw}' isn't a number")
            return None
        if value < lo:
            errors.append(f"{label} must be at least {lo}")
        return value

    monitor_targets = {}
    for name in targets:
        on = form.get(f"on_{name}") == "on"
        interval = number(f"interval_{name}", int, MIN_INTERVAL, f"{name} interval") if on else 0
        monitor_targets[name] = interval or 0
    if not any(monitor_targets.values()):
        errors.append("Turn on at least one target.")

    discord = dict(local.get("discord") or {})
    discord["mention"] = (form.get("mention") or "").strip()
    discord["notify_changes"] = form.get("notify_changes") == "on"
    discord["notify_blocks"] = form.get("notify_blocks") == "on"
    webhook = (form.get("webhook_url") or "").strip()
    if form.get("clear_webhook") == "on":
        discord["webhook_url"] = None
    elif webhook and webhook != SECRET_MASK:
        if not webhook.startswith("https://discord.com/api/webhooks/"):
            errors.append("The webhook URL should start with https://discord.com/api/webhooks/")
        discord["webhook_url"] = webhook

    monitor = dict(local.get("monitor") or {})
    monitor.update({
        "targets": monitor_targets,
        "cooldown_minutes": number("cooldown_minutes", int, 1, "Cooldown"),
        "max_cooldown_minutes": number("max_cooldown_minutes", int, 1, "Max cooldown"),
        "heartbeat_hours": number("heartbeat_hours", int, 0, "Heartbeat"),
        "jitter": number("jitter", float, 0, "Jitter"),
    })
    max_price = number("max_price", Decimal, 1, "Max price")

    credentials = dict(local.get("credentials") or {})
    bestbuy = (form.get("bestbuy_api_key") or "").strip()
    if form.get("clear_bestbuy_api_key") == "on":
        credentials["bestbuy_api_key"] = None
    elif bestbuy and bestbuy != SECRET_MASK:
        credentials["bestbuy_api_key"] = bestbuy
    if "walmart_consumer_id" in form:
        credentials["walmart_consumer_id"] = (form.get("walmart_consumer_id") or "").strip() or None
        credentials["walmart_key_version"] = (form.get("walmart_key_version") or "").strip() or "1"
    if errors:
        return errors
    if monitor["max_cooldown_minutes"] < monitor["cooldown_minutes"]:
        return ["Max cooldown must be at least the cooldown."]
    if monitor["jitter"] >= 1:
        return ["Jitter is a fraction: use something like 0.15."]

    new = {**local, "max_price": float(max_price), "discord": discord, "monitor": monitor,
           "credentials": credentials}
    try:
        options = config.from_dict(config._merge(config._read(config.ROOT / "config.json"), new))
    except (config.ConfigError, TypeError, ValueError) as ex:
        return [str(ex)]
    # The monitor would only skip an API target without keys, so catch it here.
    for t in options.targets:
        if t.name in options.monitor.targets and (missing := missing_credentials(t, options.credentials)):
            errors.append(f"{t.name} needs {missing} (see Retailer API keys), or turn it off.")
    if errors:
        return errors
    _write(new)
    return []


def missing_credentials(t: config.Target, c: config.Credentials) -> str | None:
    """What an API target still needs before it can run, or None."""
    if t.method == "bestbuy_api" and not c.bestbuy_api_key:
        return "a Best Buy API key"
    if t.method == "walmart_affiliate_api":
        if not c.walmart_consumer_id:
            return "a Walmart consumer ID"
        if not c.walmart_private_key_path or not Path(c.walmart_private_key_path).exists():
            return "a Walmart key pair"
    return None


def _write(new: dict[str, Any]) -> None:
    # The webhook and API keys are secrets: keep the file private. The monitor reloads when it changes.
    tmp = LOCAL.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, LOCAL)


# --- retailer API keys ---

def generate_walmart_key() -> None:
    """Makes a new RSA key pair for Walmart.io and points the config at it, replacing any old key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    WALMART_KEY.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = WALMART_KEY.with_suffix(".tmp")
    tmp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
    os.chmod(tmp, 0o600)
    os.replace(tmp, WALMART_KEY)
    local = _local()
    local["credentials"] = {**(local.get("credentials") or {}), "walmart_private_key_path": str(WALMART_KEY)}
    _write(local)


def public_key(private_key_path: str | None) -> str | None:
    """The PEM public key to paste into the Walmart.io dashboard, or None without a usable private key."""
    try:
        key = serialization.load_pem_private_key(Path(private_key_path).read_bytes(), password=None)
    except (TypeError, OSError, ValueError):
        return None
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def test_api(name: str) -> tuple[bool, str]:
    """One request to an API target with the saved keys. Returns (worked, what happened)."""
    options = config.load()
    t = next((t for t in options.targets if t.name == name and t.method in API_METHODS), None)
    if t is None:
        return False, f"{name} isn't an API target"
    if missing := missing_credentials(t, options.credentials):
        return False, f"Save {missing} first"

    async def once():
        closers: list = []
        try:
            fetch, skip = build_fetch(t, options, None, closers)
            return (await fetch()) if fetch else skip
        finally:
            for close in closers:
                await close()

    try:
        f = asyncio.run(once())
    except Exception as ex:  # a bad key file, say
        return False, f"{type(ex).__name__}: {ex}"
    if isinstance(f, str):
        return False, f
    if f.status in (401, 403):
        return False, f"{f.status}: the key was refused. {f.body.strip()[:120]}"
    if (challenge := detect(f)) is not Challenge.NONE:
        return False, f"{f.status} {challenge}"
    parse = safe_parse(PARSERS[t.parser], f.body, t)
    if not parse.signal_found:
        return False, f"{f.status}, but no stock signal: {parse.note or f.error}"
    return True, f"Works: {f.status} in {f.latency_ms} ms, {'in' if parse.in_stock else 'out of'} stock"
