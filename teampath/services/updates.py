"""Weekly progress updates shared with leaders, and their feedback."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, update

from .. import audit
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import tenant_session
from ..models import ActivityType, Feedback, Goal, Membership, OpportunityShare, ProgressUpdate, Thread, User
from . import paths as paths_service
from .activity import week_totals_by_type
from .notify import notify
from .orgs import upline_chain

FEEDBACK_KINDS = ("comment", "coaching", "kudos")


def current_period(today: date | None = None) -> tuple[date, date]:
    """Monday..Sunday of the current week."""
    today = today or date.today()
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def draft_snapshot(ctx: TenantContext, period: tuple[date, date] | None = None) -> dict:
    start, end = period or current_period()
    with tenant_session(ctx) as db:
        types = list(db.scalars(select(ActivityType).where(ActivityType.org_id == ctx.org_id, ActivityType.active.is_(True))
                                .order_by(ActivityType.position)))
        totals = week_totals_by_type(db, ctx.user_id, start, end)
        goals = list(db.scalars(select(Goal).where(Goal.user_id == ctx.user_id, Goal.status.in_(["active", "proposed"]))))
        asked = db.scalar(select(Thread.id).where(Thread.author_id == ctx.user_id,
                                                  Thread.created_at >= datetime.combine(start, datetime.min.time()))
                          .limit(1)) is not None
        shared = len(db.scalars(select(OpportunityShare.id).where(
            OpportunityShare.from_user_id == ctx.user_id,
            OpportunityShare.created_at >= datetime.combine(start, datetime.min.time()))).all())
    paths = [{"title": e.title, "percent": e.percent, "done": e.done, "total": e.total}
             for e in paths_service.my_enrollments(ctx) if e.status == "active"]
    return {
        "period": [start.isoformat(), end.isoformat()],
        "activity": [{"label": t.label, "total": int(totals.get(t.id, 0)), "target": t.daily_target * 5} for t in types],
        "goals": [{"title": g.title, "progress": g.progress, "status": g.status} for g in goals],
        "paths": paths,
        "asked_questions": asked,
        "opportunities_shared": shared,
    }


def possible_recipients(ctx: TenantContext) -> list[tuple[uuid.UUID, str, bool]]:
    """(user_id, name, default) — the upline chain; the direct leader is the default."""
    with tenant_session(ctx) as db:
        me = db.get(Membership, ctx.membership_id)
        chain = upline_chain(db, ctx.org_id, me.path)
        names = dict(db.execute(select(User.id, User.name).where(User.id.in_([m.user_id for m in chain]))).all()) if chain else {}
        return [(m.user_id, names.get(m.user_id, ""), i == 0) for i, m in enumerate(chain)]


def send_update(ctx: TenantContext, *, wins: str = "", blockers: str = "", help_wanted: str = "",
                recipient_ids: list[uuid.UUID] | None = None) -> uuid.UUID:
    allowed = {uid for uid, _, _ in possible_recipients(ctx)}
    if recipient_ids is None:
        recipient_ids = [uid for uid, _, default in possible_recipients(ctx) if default]
    if not set(recipient_ids) <= allowed:
        raise PermissionDenied("Updates can go only to leaders above you.")
    if not recipient_ids:
        raise ValidationError("There's no leader above you to send this to.")
    if not any(s.strip() for s in (wins, blockers, help_wanted)):
        raise ValidationError("Add at least a line about your week.")
    snap = draft_snapshot(ctx)
    start, end = current_period()
    with tenant_session(ctx) as db:
        u = ProgressUpdate(org_id=ctx.org_id, user_id=ctx.user_id, period_start=start, period_end=end, snapshot=snap,
                           wins=wins.strip() or None, blockers=blockers.strip() or None,
                           help_wanted=help_wanted.strip() or None, recipients=list(recipient_ids))
        db.add(u)
        db.flush()
        for rid in recipient_ids:
            notify(db, ctx, rid, "update", f"Weekly update from {ctx.user_name}", route=f"/updates/{u.id}")
        audit.record(db, ctx, "update.sent", "progress_update", u.id, recipients=[str(r) for r in recipient_ids])
        _changed(db, ctx, u.id)
        return u.id


@dataclass
class FeedbackView:
    id: uuid.UUID
    author_name: str
    kind: str
    body: str
    created_at: datetime


@dataclass
class UpdateView:
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str
    period_start: date
    period_end: date
    snapshot: dict
    wins: str | None
    blockers: str | None
    help_wanted: str | None
    created_at: datetime
    feedback: list[FeedbackView]
    feedback_count: int
    is_mine: bool


def _views(db, ctx, rows: list[ProgressUpdate], with_feedback=False) -> list[UpdateView]:
    ids = {r.user_id for r in rows}
    fb: dict[uuid.UUID, list[Feedback]] = {}
    if with_feedback and rows:
        for f in db.scalars(select(Feedback).where(Feedback.update_id.in_([r.id for r in rows])).order_by(Feedback.created_at)):
            fb.setdefault(f.update_id, []).append(f)
            ids.add(f.author_id)
    names = dict(db.execute(select(User.id, User.name).where(User.id.in_(ids))).all()) if ids else {}
    return [UpdateView(r.id, r.user_id, names.get(r.user_id, ""), r.period_start, r.period_end, r.snapshot or {},
                       r.wins, r.blockers, r.help_wanted, r.created_at,
                       [FeedbackView(f.id, names.get(f.author_id, ""), f.kind, f.body, f.created_at) for f in fb.get(r.id, [])],
                       r.feedback_count, r.user_id == ctx.user_id) for r in rows]


def my_updates(ctx: TenantContext) -> list[UpdateView]:
    with tenant_session(ctx) as db:
        rows = list(db.scalars(select(ProgressUpdate).where(ProgressUpdate.user_id == ctx.user_id)
                               .order_by(ProgressUpdate.created_at.desc()).limit(30)))
        return _views(db, ctx, rows)


def leader_inbox(ctx: TenantContext, awaiting_only: bool = False) -> list[UpdateView]:
    with tenant_session(ctx) as db:
        q = select(ProgressUpdate).where(ProgressUpdate.org_id == ctx.org_id, ProgressUpdate.user_id != ctx.user_id)
        if awaiting_only:
            q = q.where(ProgressUpdate.feedback_count == 0)
        rows = list(db.scalars(q.order_by(ProgressUpdate.created_at.desc()).limit(100)))
        return _views(db, ctx, rows)


def person_updates(ctx: TenantContext, user_id: uuid.UUID) -> list[UpdateView]:
    with tenant_session(ctx) as db:
        rows = list(db.scalars(select(ProgressUpdate).where(ProgressUpdate.user_id == user_id)
                               .order_by(ProgressUpdate.created_at.desc()).limit(12)))
        return _views(db, ctx, rows)


def get_update(ctx: TenantContext, update_id: uuid.UUID) -> UpdateView:
    with tenant_session(ctx) as db:
        u = db.get(ProgressUpdate, update_id)
        if not u:
            raise NotFound("Update not found")
        view = _views(db, ctx, [u], True)[0]
        if u.user_id == ctx.user_id:
            # The member has now seen the feedback on their update.
            db.execute(update(Feedback).where(Feedback.update_id == u.id, Feedback.author_id != ctx.user_id,
                                              Feedback.seen_at.is_(None)).values(seen_at=func.now()))
        return view


@dataclass
class NewFeedback:
    update_id: uuid.UUID
    period_start: date
    author_name: str
    kind: str
    body: str
    created_at: datetime


def unseen_feedback(ctx: TenantContext) -> list[NewFeedback]:
    """Feedback on my updates that I haven't opened yet (shown on Today)."""
    with tenant_session(ctx) as db:
        rows = db.execute(
            select(Feedback, ProgressUpdate.period_start, User.name)
            .join(ProgressUpdate, ProgressUpdate.id == Feedback.update_id)
            .join(User, User.id == Feedback.author_id)
            .where(Feedback.update_owner_id == ctx.user_id, Feedback.author_id != ctx.user_id,
                   Feedback.seen_at.is_(None))
            .order_by(Feedback.created_at.desc()).limit(10)
        ).all()
        return [NewFeedback(f.update_id, start, name, f.kind, f.body, f.created_at) for f, start, name in rows]


def _changed(db, ctx: TenantContext, update_id) -> None:
    from .. import events
    from .notify import after_commit

    after_commit(db, lambda: events.publish(events.org_topic(ctx.org_id), kind="updates", update_id=str(update_id)))


def add_feedback(ctx: TenantContext, update_id: uuid.UUID, body: str, kind: str = "comment") -> None:
    body = (body or "").strip()
    if not body:
        raise ValidationError("Write your feedback.")
    if kind not in FEEDBACK_KINDS:
        raise ValidationError("Unknown feedback type.")
    with tenant_session(ctx) as db:
        u = db.get(ProgressUpdate, update_id)
        if not u:
            raise NotFound("Update not found")
        db.add(Feedback(org_id=ctx.org_id, update_id=u.id, update_owner_id=u.user_id, author_id=ctx.user_id,
                        kind=kind, body=body))
        if ctx.user_id != u.user_id:
            u.feedback_count += 1
            label = {"kudos": "Kudos", "coaching": "Coaching note"}.get(kind, "Feedback")
            notify(db, ctx, u.user_id, "feedback", f"{label} from {ctx.user_name} on your weekly update",
                   route=f"/updates/{u.id}")
        else:
            for rid in u.recipients or []:
                notify(db, ctx, rid, "update_reply", f"{ctx.user_name} replied on their update", route=f"/updates/{u.id}")
        audit.record(db, ctx, "update.feedback", "progress_update", u.id, kind=kind)
        _changed(db, ctx, u.id)
