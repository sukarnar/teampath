"""In-app notifications (and live pushes to open sessions after commit)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import event, insert, select, update
from sqlalchemy.orm import Session

from .. import events
from ..context import TenantContext
from ..db import tenant_session
from ..models import Notification


@event.listens_for(Session, "after_commit")
def _run_after_commit(session):
    for fn in session.info.pop("after_commit", []):
        fn()


@event.listens_for(Session, "after_rollback")
def _drop_after_rollback(session):
    session.info.pop("after_commit", None)


def after_commit(db: Session, fn) -> None:
    db.info.setdefault("after_commit", []).append(fn)


def notify(db: Session, ctx: TenantContext, user_id: uuid.UUID, kind: str, title: str,
           body: str | None = None, route: str | None = None, org_id=None) -> None:
    org_id = org_id or ctx.org_id
    # Core insert (no RETURNING): the sender may create but not read the recipient's notification.
    db.execute(insert(Notification.__table__).values(org_id=org_id, user_id=user_id, kind=kind, title=title[:200], body=body,
                                           route=route))
    after_commit(db, lambda: events.publish(events.user_topic(user_id), kind="notification", org_id=str(org_id)))


def my_notifications(ctx: TenantContext, limit: int = 30) -> list[Notification]:
    with tenant_session(ctx) as db:
        return list(db.scalars(select(Notification).where(Notification.user_id == ctx.user_id,
                                                          Notification.org_id == ctx.org_id)
                               .order_by(Notification.created_at.desc()).limit(limit)))


def unread_count(ctx: TenantContext) -> int:
    with tenant_session(ctx) as db:
        return len(db.scalars(select(Notification.id).where(Notification.user_id == ctx.user_id,
                                                            Notification.org_id == ctx.org_id,
                                                            Notification.read_at.is_(None))).all())


def mark_all_read(ctx: TenantContext) -> None:
    with tenant_session(ctx) as db:
        db.execute(update(Notification).where(Notification.user_id == ctx.user_id, Notification.org_id == ctx.org_id,
                                              Notification.read_at.is_(None))
                   .values(read_at=datetime.now(timezone.utc)))
