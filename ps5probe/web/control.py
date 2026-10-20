"""Changes the UI makes: monitor commands (via its inbox) and settings (via config.local.json)."""

from __future__ import annotations

import json
import os
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any


from .. import config
from .data import monitor_dir

COMMANDS = ("check_now", "pause", "resume", "clear_cooldown")
MIN_INTERVAL = 5
SECRET_MASK = "••••••••"  # stands in for a saved secret in the form; sent back unchanged, it means "keep it"


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
    if errors:
        return errors
    if monitor["max_cooldown_minutes"] < monitor["cooldown_minutes"]:
        return ["Max cooldown must be at least the cooldown."]
    if monitor["jitter"] >= 1:
        return ["Jitter is a fraction: use something like 0.15."]

    new = {**local, "max_price": float(max_price), "discord": discord, "monitor": monitor}
    try:
        config.from_dict(config._merge(config._read(config.ROOT / "config.json"), new))
    except (config.ConfigError, TypeError, ValueError) as ex:
        return [str(ex)]
    _write(new)
    return []


def _write(new: dict[str, Any]) -> None:
    # The webhook is a secret: keep the file private. The monitor reloads when it changes.
    tmp = LOCAL.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, LOCAL)

