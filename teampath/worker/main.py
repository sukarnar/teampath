"""Worker process: `python -m teampath.worker.main`."""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from .. import events
from ..db import system_session
from ..models import Job
from .tasks import PERIODIC, TASKS, Retry

log = logging.getLogger("teampath.worker")
MAX_ATTEMPTS = 5
_stop = False


def _now():
    return datetime.now(timezone.utc)


def claim_one() -> tuple[int, str, dict] | None:
    with system_session("worker: claim job") as db:
        job = db.scalar(select(Job).where(Job.status == "queued", Job.run_after <= _now())
                        .order_by(Job.id).limit(1).with_for_update(skip_locked=True))
        if not job:
            return None
        job.status = "running"
        job.attempts += 1
        return job.id, job.kind, dict(job.payload or {})


def finish(job_id: int, error: str | None = None, retry_in: int | None = None) -> None:
    with system_session("worker: finish job") as db:
        job = db.get(Job, job_id)
        if error is None and retry_in is None:
            job.status = "done"
        elif retry_in is not None and job.attempts < MAX_ATTEMPTS * 4:
            job.status = "queued"
            job.run_after = _now() + timedelta(seconds=retry_in)
        elif job.attempts < MAX_ATTEMPTS:
            job.status = "queued"
            job.run_after = _now() + timedelta(seconds=30 * 2 ** job.attempts)
            job.last_error = (error or "")[:2000]
        else:
            job.status = "failed"
            job.last_error = (error or "")[:2000]


def run_once() -> bool:
    claimed = claim_one()
    if not claimed:
        return False
    job_id, kind, payload = claimed
    fn = TASKS.get(kind)
    try:
        if not fn:
            raise RuntimeError(f"unknown job kind {kind}")
        fn(**payload)
        finish(job_id)
    except Retry as r:
        finish(job_id, retry_in=r.seconds)
    except Exception as e:  # noqa: BLE001
        log.exception("job %s (%s) failed", job_id, kind)
        finish(job_id, error=repr(e))
    return True


def drain(max_jobs: int = 1000) -> int:
    """Run queued jobs until none are ready (used by tests and the CLI)."""
    n = 0
    while n < max_jobs and run_once():
        n += 1
    return n


def schedule_periodic(last_run: dict) -> None:
    now = _now()
    for kind, every in PERIODIC:
        if now - last_run.get(kind, datetime.min.replace(tzinfo=timezone.utc)) >= every:
            with system_session("worker: schedule periodic job") as db:
                pending = db.execute(text("select 1 from jobs where kind = :k and status in ('queued','running') limit 1"),
                                     {"k": kind}).first()
                if not pending:
                    db.add(Job(kind=kind, payload={}))
            last_run[kind] = now


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    def stop(*_):
        global _stop
        _stop = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    events.use_postgres_notify()  # UI events go to the web app via Postgres
    log.info("worker started")
    with system_session("worker: recover jobs interrupted by a restart") as db:
        db.execute(text("update jobs set status = 'queued' where status = 'running'"))
    last_run: dict = {}
    wake = _JobWakeup()
    while not _stop:
        try:
            schedule_periodic(last_run)
            if not run_once():
                wake.wait(5)  # returns as soon as a new job is queued
        except Exception:  # database restarts etc.
            log.exception("worker loop error")
            time.sleep(5)
    log.info("worker stopped")


class _JobWakeup:
    """LISTENs for new-job notifications so queued work starts immediately."""

    def __init__(self):
        self.conn = None

    def wait(self, timeout: float) -> None:
        import psycopg

        try:
            if self.conn is None or self.conn.closed:
                self.conn = psycopg.connect(events._raw_dsn(), autocommit=True)
                self.conn.execute(f"LISTEN {events.JOBS_CHANNEL}")
            for _ in self.conn.notifies(timeout=timeout, stop_after=1):
                pass
        except Exception:
            log.warning("job wake-up listener failed; polling instead", exc_info=True)
            self.conn = None
            time.sleep(min(timeout, 2))


if __name__ == "__main__":
    main()
