"""Enqueue background jobs (processed by teampath.worker)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from .events import JOBS_CHANNEL
from .models import Job


def enqueue(db: Session, kind: str, **payload) -> None:
    db.add(Job(kind=kind, payload={k: str(v) if v is not None else None for k, v in payload.items()}))
    # Delivered when the transaction commits; wakes the worker immediately.
    db.execute(text("select pg_notify(:c, '')"), {"c": JOBS_CHANNEL})
