"""Bounded browser diagnostics and background jobs, without console handlers."""
from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime

_CURRENT_JOB: ContextVar[Job | None] = ContextVar("wxmoments_job", default=None)
_EVENTS: deque[dict] = deque(maxlen=400)
_LOCK = threading.RLock()
_CURSOR = 0
JOBS: dict[str, Job] = {}


class Job:
    def __init__(self, total: int):
        self.id = uuid.uuid4().hex[:12]
        self.total = total
        self.done = 0
        self.status = "running"
        self.message = "准备中…"
        self.log: deque[str] = deque(maxlen=120)
        self.results: list[dict] = []
        self.canceled = False
        self._lock = threading.RLock()

    def add_log(self, text: str, *, publish: bool = True) -> None:
        with self._lock:
            self.log.append(f"[{datetime.now():%H:%M:%S}] {text}")
        if publish:
            logging.getLogger("task").info(text, extra={"job_log": True, "job_id": self.id})

    def check_canceled(self) -> None:
        if self.canceled:
            raise RuntimeError("已取消")

    def finish(self, status: str, message: str) -> None:
        with self._lock:
            self.message = message
            self.add_log(message)
            self.status = status

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id, "total": self.total, "done": self.done,
                "status": self.status, "message": self.message,
                "log": list(self.log), "results": list(self.results),
            }


class BrowserLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _CURSOR
        message = self.format(record)
        job = _CURRENT_JOB.get()
        if job and not getattr(record, "job_log", False):
            job.add_log(message, publish=False)
        with _LOCK:
            _CURSOR += 1
            _EVENTS.append({
                "id": _CURSOR, "time": datetime.now().strftime("%H:%M:%S"),
                "level": record.levelname, "message": message,
                # Only explicitly written user messages become floating notices.
                "notice": getattr(record, "browser_notice", ""),
                "job": getattr(record, "job_id", job.id if job else ""),
            })


def install_browser_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, BrowserLogHandler) for h in root.handlers):
        handler = BrowserLogHandler()
        handler.setFormatter(logging.Formatter("%(name)s · %(message)s"))
        root.addHandler(handler)


def diagnostics(after: int = 0) -> dict:
    with _LOCK:
        return {"ok": True, "cursor": _CURSOR,
                "events": [dict(e) for e in _EVENTS if e["id"] > after]}


def start_job(total: int, target, *args) -> Job:
    job = Job(total)
    with _LOCK:
        # Retain all running jobs and the latest 80 completed jobs.
        completed = [key for key, value in JOBS.items() if value.status != "running"]
        for key in completed[:-79]:
            JOBS.pop(key, None)
        JOBS[job.id] = job

    def run() -> None:
        token = _CURRENT_JOB.set(job)
        try:
            target(job, *args)
        except Exception as exc:
            job.finish("error", f"任务失败：{exc}")
            logging.getLogger(__name__).exception(job.message)
        finally:
            _CURRENT_JOB.reset(token)

    threading.Thread(target=run, daemon=True, name=f"job-{job.id}").start()
    return job
