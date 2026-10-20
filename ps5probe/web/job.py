"""Manual Phase 1 probe runs: `python -m ps5probe` as a subprocess, one at a time."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from .. import config
from .data import runs_dir


class Job:
    """The one probe subprocess the UI may have running, and its console output.

    Its pid, args and log path are kept in runs/.web/job.json, so a reloaded server (auto-reload on
    code changes) picks the run back up instead of losing track of it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.args: list[str] = []
        self.started: datetime | None = None
        self.log_path: Path | None = None
        self.exit_code: int | None = None
        self._load()

    @staticmethod
    def _state_path() -> Path:
        return runs_dir() / ".web" / "job.json"

    def _load(self) -> None:
        try:
            state = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.pid, self.args, self.exit_code = state["pid"], state["args"], state.get("exit_code")
        self.started = datetime.fromisoformat(state["started"])
        self.log_path = Path(state["log_path"])

    def _save(self) -> None:
        self._state_path().write_text(json.dumps({
            "pid": self.pid, "args": self.args, "started": self.started.isoformat(),
            "log_path": str(self.log_path), "exit_code": self.exit_code,
        }), encoding="utf-8")

    @property
    def running(self) -> bool:
        if self.proc is not None:  # started by this process: poll() also reaps it
            if self.proc.poll() is None:
                return True
            if self.exit_code is None:
                self.exit_code = self.proc.returncode
                self._save()
            return False
        if self.pid is None or self.exit_code is not None:
            return False
        # Started before a reload: alive if the pid still belongs to a probe.
        try:
            alive = b"ps5probe" in Path(f"/proc/{self.pid}/cmdline").read_bytes()
        except OSError:
            alive = False
        return alive

    def start(self, args: list[str]) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("a run is already in progress")
            logs = runs_dir() / ".web"
            logs.mkdir(parents=True, exist_ok=True)
            self.started = datetime.now()
            self.args = args
            self.exit_code = None
            self.log_path = logs / f"{self.started:%Y%m%d-%H%M%S}.log"
            with open(self.log_path, "w", encoding="utf-8") as out:
                # New session so SIGINT reaches only the probe, which then writes its summary,
                # and so a server reload doesn't take the run down with it.
                self.proc = subprocess.Popen(
                    [sys.executable, "-u", "-m", "ps5probe", *args],
                    cwd=config.ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                )
            self.pid = self.proc.pid
            self._save()

    def stop(self) -> None:
        if self.running:
            os.kill(self.pid, signal.SIGINT)

    def output(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path else ""
        except OSError:
            return ""

    def status(self) -> dict:
        running = self.running
        return {
            "running": running,
            "args": self.args,
            "started": self.started.isoformat(timespec="seconds") if self.started else None,
            "exit_code": self.exit_code,
            "output": self.output()[-20000:],
        }


job = Job()
