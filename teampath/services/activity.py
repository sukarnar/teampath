"""Daily activity logging, targets and momentum."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from .. import audit, events
from ..context import NotFound, TenantContext, ValidationError
from ..db import tenant_session
from ..models import ActivityLog, ActivityType, Membership, Momentum
from .notify import after_commit


@dataclass
class ActivityTile:
    type_id: uuid.UUID
    label: str
    today: int
    week: int
    daily_target: int

    @property
    def target_met(self) -> bool:
        return self.daily_target > 0 and self.today >= self.daily_target


def log(ctx: TenantContext, type_id: uuid.UUID, count: int = 1, note: str = "", day: date | None = None) -> None:
    if count == 0 or abs(count) > 500:
        raise ValidationError("Enter a count between 1 and 500.")
    today = ctx.today()
    day = day or today
    if day > today or day < today - timedelta(days=7):
        raise ValidationError("You can log activity for the past week only.")
    with tenant_session(ctx) as db:
        t = db.get(ActivityType, type_id)
        if not t or not t.active:
            raise NotFound("Activity type not found")
        if count < 0:
            # Undo: remove the most recent entries for today, never below zero.
            today_total = db.scalar(select(func.coalesce(func.sum(ActivityLog.count), 0)).where(
                ActivityLog.user_id == ctx.user_id, ActivityLog.type_id == t.id, ActivityLog.logged_on == day)) or 0
            count = -min(-count, today_total)
            if count == 0:
                return
        db.add(ActivityLog(org_id=ctx.org_id, user_id=ctx.user_id, type_id=t.id, count=count, logged_on=day,
                           note=(note or "").strip()[:300] or None))
        db.flush()
        refresh_momentum(db, ctx.org_id, ctx.user_id, today)
        after_commit(db, lambda: events.publish(events.org_topic(ctx.org_id), kind="activity", user_id=str(ctx.user_id)))


def tiles(ctx: TenantContext, user_id: uuid.UUID | None = None) -> list[ActivityTile]:
    uid = user_id or ctx.user_id
    today = ctx.today()
    week_start = today - timedelta(days=6)
    with tenant_session(ctx) as db:
        types = list(db.scalars(select(ActivityType).where(ActivityType.org_id == ctx.org_id, ActivityType.active.is_(True))
                                .order_by(ActivityType.position)))
        rows = db.execute(select(ActivityLog.type_id, ActivityLog.logged_on, func.sum(ActivityLog.count))
                          .where(ActivityLog.user_id == uid, ActivityLog.logged_on >= week_start)
                          .group_by(ActivityLog.type_id, ActivityLog.logged_on)).all()
    today_by, week_by = {}, {}
    for tid, d, n in rows:
        week_by[tid] = week_by.get(tid, 0) + n
        if d == today:
            today_by[tid] = n
    return [ActivityTile(t.id, t.label, today_by.get(t.id, 0), week_by.get(t.id, 0), t.daily_target) for t in types]


def daily_totals(ctx: TenantContext, user_id: uuid.UUID | None = None, days: int = 14) -> list[tuple[date, int]]:
    uid = user_id or ctx.user_id
    start = ctx.today() - timedelta(days=days - 1)
    with tenant_session(ctx) as db:
        rows = dict(db.execute(select(ActivityLog.logged_on, func.sum(ActivityLog.count))
                               .where(ActivityLog.user_id == uid, ActivityLog.logged_on >= start)
                               .group_by(ActivityLog.logged_on)).all())
    return [(start + timedelta(days=i), int(rows.get(start + timedelta(days=i), 0))) for i in range(days)]


def week_totals_by_type(db, user_id, start: date, end: date) -> dict[uuid.UUID, int]:
    return dict(db.execute(select(ActivityLog.type_id, func.sum(ActivityLog.count))
                           .where(ActivityLog.user_id == user_id, ActivityLog.logged_on.between(start, end))
                           .group_by(ActivityLog.type_id)).all())


def momentum_status(week_total: int, baseline: float, last_on: date | None, joined: date, today: date) -> str:
    """Compares this week with the person's own recent average (not with other people)."""
    if last_on is None:
        return "new" if (today - joined).days < 14 else "inactive"
    idle = (today - last_on).days
    if idle >= 7 or week_total == 0:
        return "inactive"
    if idle >= 4 or (baseline >= 5 and week_total < 0.6 * baseline):
        return "slowing"
    return "active"


def refresh_momentum(db, org_id, user_id, today: date | None = None) -> str:
    today = today or date.today()
    week = db.scalar(select(func.coalesce(func.sum(ActivityLog.count), 0)).where(
        ActivityLog.org_id == org_id, ActivityLog.user_id == user_id, ActivityLog.logged_on > today - timedelta(days=7))) or 0
    prior = db.scalar(select(func.coalesce(func.sum(ActivityLog.count), 0)).where(
        ActivityLog.org_id == org_id, ActivityLog.user_id == user_id,
        ActivityLog.logged_on.between(today - timedelta(days=35), today - timedelta(days=8)))) or 0
    last_on = db.scalar(select(func.max(ActivityLog.logged_on)).where(
        ActivityLog.org_id == org_id, ActivityLog.user_id == user_id, ActivityLog.count > 0))
    joined = db.scalar(select(Membership.joined_at).where(Membership.org_id == org_id, Membership.user_id == user_id))
    status = momentum_status(int(week), prior / 4.0, last_on, (joined or datetime.now(timezone.utc)).date(), today)
    stmt = insert(Momentum).values(org_id=org_id, user_id=user_id, week_total=int(week), baseline_weekly=prior / 4.0,
                                   last_activity_on=last_on, status=status, computed_at=datetime.now(timezone.utc))
    db.execute(stmt.on_conflict_do_update(index_elements=["org_id", "user_id"], set_={
        "week_total": stmt.excluded.week_total, "baseline_weekly": stmt.excluded.baseline_weekly,
        "last_activity_on": stmt.excluded.last_activity_on, "status": stmt.excluded.status,
        "computed_at": stmt.excluded.computed_at}))
    return status


def clear_my_day(ctx: TenantContext, type_id: uuid.UUID, day: date) -> None:
    with tenant_session(ctx) as db:
        db.execute(delete(ActivityLog).where(ActivityLog.user_id == ctx.user_id, ActivityLog.type_id == type_id,
                                             ActivityLog.logged_on == day))
        refresh_momentum(db, ctx.org_id, ctx.user_id)
        audit.record(db, ctx, "activity.cleared", "activity_type", type_id, day=str(day))
