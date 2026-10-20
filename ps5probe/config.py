"""Settings: config.json, then config.local.json over it, then PS5_PROBE__* environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

# The checkout: holds config.json, and relative output dirs land here wherever you run from.
ROOT = Path(__file__).resolve().parent.parent
ENV_PREFIX = "PS5_PROBE__"

ON_CHALLENGE = ("stop", "cooldown_then_hold")
METHODS = ("http", "browser", "bestbuy_api", "walmart_affiliate_api")


class ConfigError(Exception):
    pass


@dataclass
class Ramp:
    # Descending poll intervals; each step runs samples_per_step samples.
    intervals_seconds: list[int] = field(default_factory=lambda: [120, 60, 30, 15])
    samples_per_step: int = 10
    jitter: float = 0.15
    # "stop": done for the run. "cooldown_then_hold": pause, run one step at the last clean interval, stop.
    on_challenge: str = "stop"
    cooldown_minutes: int = 60

    def __post_init__(self) -> None:
        if self.on_challenge not in ON_CHALLENGE:
            raise ConfigError(f"on_challenge must be one of {', '.join(ON_CHALLENGE)}, not '{self.on_challenge}'")
        if not self.intervals_seconds:
            self.intervals_seconds = Ramp().intervals_seconds


@dataclass
class Target:
    name: str = ""
    retailer: str = ""
    method: str = "http"
    url: str = ""
    parser: str = ""
    # Link to put in an alert; defaults to url.
    product_url: str | None = None
    enabled: bool = True
    params: dict[str, str] = field(default_factory=dict)
    # HTTP only: extra request headers.
    headers: dict[str, str] = field(default_factory=dict)
    # Seller names (as the parser reports them) that count as the retailer itself.
    allowed_sellers: list[str] = field(default_factory=list)
    ramp: Ramp = field(default_factory=Ramp)
    # Browser only: selector to wait for after DOMContentLoaded (up to settle_ms).
    wait_for_selector: str | None = None
    settle_ms: int = 3000

    def param(self, key: str) -> str:
        if key not in self.params:
            raise ValueError(f"Target '{self.name}' needs params.{key}")
        return self.params[key]


@dataclass
class Credentials:
    bestbuy_api_key: str | None = None
    walmart_consumer_id: str | None = None
    walmart_key_version: str = "1"
    walmart_private_key_path: str | None = None


@dataclass
class Discord:
    # Channel webhook (Server settings > Integrations > Webhooks). Unset: notifications only go to the console.
    webhook_url: str | None = None
    # Prepended to stock alerts only, e.g. "@here" or "<@&role_id>". Other messages never ping.
    mention: str = "@here"
    # Also post when a stock signal changes but doesn't meet the alert rule (catches a parser misreading stock).
    notify_changes: bool = True
    # Also post blocks, cooldowns and recoveries.
    notify_blocks: bool = True


@dataclass
class Monitor:
    # Target name -> poll interval in seconds. Disabled targets still run if named here.
    targets: dict[str, int] = field(default_factory=dict)
    jitter: float = 0.15
    # After a block, pause the whole retailer; each further block in a row doubles it, up to the max.
    cooldown_minutes: int = 30
    max_cooldown_minutes: int = 240
    # A "still running" summary post; 0 turns it off.
    heartbeat_hours: int = 24
    # Overrides the top-level headless for the monitor's browser targets (false needs a display, e.g. xvfb-run).
    headless: bool | None = None


@dataclass
class Options:
    output_dir: str = "runs"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    )
    headless: bool = True
    request_timeout_seconds: int = 20
    max_price: Decimal = Decimal("899.99")
    # Consecutive non-challenge failures (5xx, network errors, unparsable pages) that count as a block.
    max_consecutive_errors: int = 3
    max_run_minutes: int | None = None
    credentials: Credentials = field(default_factory=Credentials)
    discord: Discord = field(default_factory=Discord)
    monitor: Monitor = field(default_factory=Monitor)
    targets: list[Target] = field(default_factory=list)
    # Named runs (--profile): which targets to run and what to override on them. Kept as raw dicts.
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)


def load(root: Path = ROOT) -> Options:
    raw = _read(root / "config.json")
    local = root / "config.local.json"
    if local.exists():
        raw = _merge(raw, _read(local))
    for key, value in os.environ.items():
        if key.upper().startswith(ENV_PREFIX):
            _set_path(raw, key[len(ENV_PREFIX):].lower().split("__"), value)
    return from_dict(raw)


def from_dict(raw: dict[str, Any]) -> Options:
    raw = dict(raw)
    # A target's ramp only overrides the fields it sets; the rest come from default_ramp.
    default_ramp = raw.pop("default_ramp", None) or {}
    targets = [_target(t, default_ramp) for t in raw.pop("targets", None) or []]
    credentials = _build(Credentials, raw.pop("credentials", None) or {}, "credentials")
    discord = _build(Discord, raw.pop("discord", None) or {}, "discord")
    monitor = _build(Monitor, raw.pop("monitor", None) or {}, "monitor")
    if "max_price" in raw:
        raw["max_price"] = Decimal(str(raw["max_price"]))
    options = _build(
        Options, {**raw, "credentials": credentials, "discord": discord, "monitor": monitor, "targets": targets}, "config"
    )
    # An interval of 0 or null turns a target off (config.local.json can't delete config.json's keys).
    options.monitor.targets = {n: int(s) for n, s in options.monitor.targets.items() if s}
    unknown = [n for n in options.monitor.targets if _find({t.name: t for t in targets}, n) is None]
    if unknown:
        raise ConfigError(f"monitor.targets names unknown target(s) {', '.join(unknown)}")
    return options


def apply_profile(options: Options, name: str) -> dict[str, Any]:
    """Layers a profile's ramp/settle/headless overrides onto options and returns the profile."""
    profile = _find(options.profiles, name)
    if profile is None:
        raise ConfigError(f"Unknown profile '{name}'. Known: {', '.join(options.profiles)}")
    for target_name, over in (profile.get("targets") or {}).items():
        t = next((t for t in options.targets if t.name.lower() == target_name.lower()), None)
        if t is None:
            raise ConfigError(f"Profile '{name}' names unknown target '{target_name}'.")
        if over.get("ramp"):
            t.ramp = _build(Ramp, {**asdict(t.ramp), **over["ramp"]}, f"profiles.{name}.targets.{target_name}.ramp")
        if over.get("settle_ms") is not None:
            t.settle_ms = over["settle_ms"]
    if profile.get("headless") is not None:
        options.headless = profile["headless"]
    if profile.get("max_run_minutes") is not None:
        options.max_run_minutes = profile["max_run_minutes"]
    return profile


def _target(raw: dict[str, Any], default_ramp: dict[str, Any]) -> Target:
    raw = dict(raw)
    where = f"targets.{raw.get('name', '?')}"
    ramp = _build(Ramp, {**default_ramp, **(raw.pop("ramp", None) or {})}, f"{where}.ramp")
    return _build(Target, {**raw, "ramp": ramp}, where)


def _build(cls: type, values: dict[str, Any], where: str) -> Any:
    unknown = set(values) - {f.name for f in fields(cls)}
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    return cls(**values)


def _find(d: dict[str, Any], name: str) -> Any:
    return next((v for k, v in d.items() if k.lower() == name.lower()), None)


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise ConfigError(f"Can't read {path}: {ex}") from ex


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Merges objects key by key; anything else (lists included) is replaced."""
    merged = dict(base)
    for k, v in over.items():
        merged[k] = _merge(merged[k], v) if isinstance(v, dict) and isinstance(merged.get(k), dict) else v
    return merged


def _set_path(d: dict[str, Any], path: list[str], value: str) -> None:
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value
