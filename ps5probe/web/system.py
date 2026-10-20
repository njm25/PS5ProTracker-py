"""systemd units, their journals, and the VM's own health. Restarts go through a narrow sudoers rule."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import config

UNITS = {"ps5probe-monitor": "Monitor", "ps5probe-web": "Web UI"}
# Exactly what deploy/ps5probe.sudoers allows; anything else would prompt for a password and fail.
ACTIONS = {"ps5probe-monitor": ("start", "stop", "restart"), "ps5probe-web": ("restart",)}


def _run(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def unit_state(unit: str) -> str:
    r = _run(["systemctl", "is-active", unit])
    return r.stdout.strip() if r else "unknown"


def unit(unit: str) -> dict[str, Any]:
    r = _run(["systemctl", "show", unit, "-p",
              "ActiveState,SubState,MainPID,NRestarts,MemoryCurrent,ActiveEnterTimestampMonotonic,UnitFileState"])
    props = dict(line.split("=", 1) for line in (r.stdout.splitlines() if r else []) if "=" in line)
    since = None
    if (mono := props.get("ActiveEnterTimestampMonotonic", "0")).isdigit() and int(mono):
        since = time.time() - (time.monotonic() - int(mono) / 1e6)
    mem = props.get("MemoryCurrent", "")
    return {
        "name": unit,
        "label": UNITS.get(unit, unit),
        "state": props.get("ActiveState", "unknown"),
        "sub": props.get("SubState", ""),
        "pid": props.get("MainPID"),
        "restarts": props.get("NRestarts"),
        "memory_mb": round(int(mem) / 2**20) if mem.isdigit() else None,
        "since": since,
        "enabled": props.get("UnitFileState"),
        "actions": ACTIONS.get(unit, ()),
    }


def journal(unit: str, lines: int = 80) -> str:
    r = _run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-q", "-o", "short-iso"])
    return r.stdout if r else ""


def control(unit: str, action: str) -> tuple[bool, str]:
    if action not in ACTIONS.get(unit, ()):
        return False, f"{action} {unit} is not allowed"
    # --no-block: restarting the web UI kills this very process, so don't wait for it.
    r = _run(["sudo", "-n", "/usr/bin/systemctl", "--no-block", action, unit], timeout=10)
    if r is None:
        return False, "sudo didn't run"
    if r.returncode != 0:
        msg = r.stderr.strip()
        if "password" in msg:
            return False, "The sudoers rule isn't installed (see System page)."
        return False, msg or f"exit {r.returncode}"
    return True, f"{action} {unit}: sent"


def sudo_ready() -> bool:
    return Path("/etc/sudoers.d/ps5probe").exists()


def vm() -> dict[str, Any]:
    mem: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    disk = shutil.disk_usage(config.ROOT)
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except OSError:
        uptime = None
    chrome = _processes("chrome")
    return {
        "cpus": os.cpu_count(),
        "load": os.getloadavg(),
        "mem_used_pct": round(100 * (total - avail) / total) if total else None,
        "mem_used_gb": round((total - avail) / 2**30, 1),
        "mem_total_gb": round(total / 2**30, 1),
        "disk_used_pct": round(100 * disk.used / disk.total),
        "disk_free_gb": round(disk.free / 2**30, 1),
        "uptime": uptime,
        "chrome_procs": len(chrome),
        "chrome_mb": round(sum(rss for _, rss in chrome) / 2**20),
        "xvfb": bool(_processes("Xvfb")),
    }


def _processes(name: str) -> list[tuple[int, int]]:
    """(pid, rss bytes) of processes whose command name contains name."""
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if name not in (d / "comm").read_text():
                continue
            rss = next(int(l.split()[1]) * 1024 for l in (d / "status").read_text().splitlines() if l.startswith("VmRSS:"))
            out.append((int(d.name), rss))
        except (OSError, StopIteration, ValueError):
            continue
    return out
