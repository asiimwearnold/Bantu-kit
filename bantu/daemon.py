"""24/7 loop. Idle = zero tokens: it only polls a folder until a task file appears."""
from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from .config import ROOT
from .llm import OutOfBudget
from .orchestrator import run_task


@contextmanager
def _daemon_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("another BANTU daemon already holds the task queue lock") from exc
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("another BANTU daemon already holds the task queue lock") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_daemon(cfg: dict, mock: bool = False, poll: int = 10) -> None:
    tasks = ROOT / "tasks"
    for d in ("inbox", "running", "done", "failed"):
        (tasks / d).mkdir(parents=True, exist_ok=True)
    with _daemon_lock(tasks / ".daemon.lock"):
        for stale in (tasks / "running").glob("*.md"):
            shutil.move(str(stale), tasks / "inbox" / stale.name)
        print("BANTU daemon started. Drop task files into tasks/inbox/ (idle costs 0 tokens). Ctrl+C to stop.")
        try:
            while True:
                queue = sorted((tasks / "inbox").glob("*.md"))
                if not queue:
                    time.sleep(poll)
                    continue
                task = Path(shutil.move(str(queue[0]), tasks / "running" / queue[0].name))
                try:
                    status = run_task(cfg, task, mock=mock)
                    shutil.move(str(task), tasks / ("done" if status == "done" else "failed") / task.name)
                except OutOfBudget:
                    shutil.move(str(task), tasks / "inbox" / task.name)
                    print("Out of free tokens for now. Sleeping 15 minutes (daily caps reset at 00:00 UTC).")
                    time.sleep(900)
                except Exception as exc:  # noqa: BLE001
                    print(f"Task crashed: {exc}")
                    shutil.move(str(task), tasks / "failed" / task.name)
        except KeyboardInterrupt:
            print("BANTU stopped.")
