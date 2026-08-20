"""Parent-side kernel bookkeeping: one kernel per user, run/reset/reap.

A kernel is a ``python -m backend.playground.kernel`` subprocess. Runs are
serialised per kernel with a lock (two tabs cannot interleave halves of two
scripts into one namespace), bounded by a wall-clock timeout, and a kernel
that exceeds it is killed and replaced — with the state loss reported, since
losing your variables is the actual cost of an infinite loop here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import settings

#: Kernels idle longer than this are reaped on the next request.
IDLE_SECONDS = 30 * 60
#: Hard ceiling on one response line from the kernel (a table of tables…).
MAX_RESPONSE_BYTES = 8_000_000

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Kernel:
    """One subprocess plus the lock and clock that manage it."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.last_used = time.time()
        self.started_at: Optional[float] = None
        self.runs = 0
        self._next_id = 1

    # ---- lifecycle ------------------------------------------------------ #
    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "backend.playground.kernel"],
            cwd=str(_REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        )
        line = self._read_line(timeout=60.0)
        if not line or not json.loads(line).get("ready"):
            self.kill()
            raise RuntimeError("The kernel failed to start")
        self.started_at = time.time()
        self.runs = 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        self.started_at = None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - it is being discarded either way
            pass

    # ---- I/O ------------------------------------------------------------ #
    def _read_line(self, timeout: float) -> Optional[str]:
        """One line from the kernel, or ``None`` on timeout — without blocking forever.

        A reader thread per call keeps this portable (select on a pipe is
        POSIX-only politics); the thread dies with the read or is abandoned
        when the kernel is killed after a timeout.
        """
        box: List[Optional[str]] = [None]

        def read() -> None:
            try:
                box[0] = self.proc.stdout.readline(MAX_RESPONSE_BYTES)
            except Exception:  # noqa: BLE001 - pipe torn down under the read
                box[0] = None

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None
        return box[0] or None

    def run(self, code: str, timeout: float) -> Dict[str, Any]:
        with self.lock:
            self.last_used = time.time()
            fresh = False
            if not self.alive():
                self.start()
                fresh = True
            req_id = self._next_id
            self._next_id += 1
            try:
                self.proc.stdin.write(json.dumps({"id": req_id, "code": code}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.kill()
                raise RuntimeError("The kernel died; run again to start a fresh one")
            line = self._read_line(timeout=timeout)
            if line is None:
                self.kill()
                return {
                    "ok": False, "elapsed": timeout, "variables": [], "restarted": True,
                    "outputs": [{"type": "error",
                                 "text": "Timed out after {:.0f}s. The kernel was killed and "
                                         "will restart on the next run — variables are lost.".format(timeout)}],
                }
            try:
                result = json.loads(line)
            except ValueError:
                self.kill()
                raise RuntimeError("The kernel returned garbage and was restarted")
            self.runs += 1
            result.pop("id", None)
            result["fresh"] = fresh
            return result

    def status(self) -> Dict[str, Any]:
        return {
            "alive": self.alive(),
            "runs": self.runs,
            "started_at": self.started_at,
            "idle_seconds": round(time.time() - self.last_used, 1),
        }


class KernelManager:
    """One kernel per user id, created on first use, reaped when idle."""

    def __init__(self) -> None:
        self._kernels: Dict[int, Kernel] = {}
        self._lock = threading.Lock()

    def _get(self, user_id: int) -> Kernel:
        with self._lock:
            self._reap_locked()
            k = self._kernels.get(user_id)
            if k is None:
                k = self._kernels[user_id] = Kernel()
            return k

    def _reap_locked(self) -> None:
        now = time.time()
        for uid, k in list(self._kernels.items()):
            if now - k.last_used > IDLE_SECONDS:
                k.kill()
                del self._kernels[uid]

    def run(self, user_id: int, code: str) -> Dict[str, Any]:
        return self._get(user_id).run(code, timeout=settings.playground_timeout_seconds)

    def reset(self, user_id: int) -> bool:
        """Kill the user's kernel. True if there was one to kill."""
        with self._lock:
            k = self._kernels.pop(user_id, None)
        if k is None:
            return False
        alive = k.alive()
        k.kill()
        return alive

    def status(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            k = self._kernels.get(user_id)
        return k.status() if k else None

    def shutdown(self) -> None:
        with self._lock:
            for k in self._kernels.values():
                k.kill()
            self._kernels.clear()


manager = KernelManager()
