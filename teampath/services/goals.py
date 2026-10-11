"""Personal goals (30/60/90-day), agreed with the person's leader."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from .. import audit
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import tenant_session
from ..models import Goal, Membership
from .notify import notify
from .orgs import upline_chain

HORIZONS = ("30", "60", "90", "custom")
STATUSES = ("proposed", "active", "done", "dropped")


def list_goals(ctx: TenantContext, user_id: uuid.UUID | None = None) -> list[Goal]:
    uid = user_id or ctx.user_id
    with tenant_session(ctx) as db:  # RLS: self, upline, admins
        return list(db.scalars(select(Goal).where(Goal.org_id == ctx.org_id, Goal.user_id == uid)
                               .order_by(Goal.status, Goal.target_date.nulls_last())))


def create_goal(ctx: TenantContext, title: str, detail: str = "", horizon: str = "30",
                target_date: date | None = None, for_user_id: uuid.UUID | None = None) -> uuid.UUID:
    title = (title or "").strip()
    if not title:
        raise ValidationError("Describe the goal.")
    if horizon not in HORIZONS:
        raise ValidationError("Choose 30, 60, 90 days or a custom date.")
    if horizon != "custom":
        target_date = date.today() + timedelta(days=int(horizon))
    elif not target_date:
        raise ValidationError("Pick a target date.")
    uid = for_user_id or ctx.user_id
    with tenant_session(ctx) as db:
        if uid != ctx.user_id:
            m = db.scalar(select(Membership).where(Membership.org_id == ctx.org_id, Membership.user_id == uid))
            if not m or not (ctx.is_admin or m.path.startswith(ctx.path + ".")):
                raise PermissionDenied("You can set goals only for your own team.")
        g = Goal(org_id=ctx.org_id, user_id=uid, title=title[:200], detail=(detail or "").strip() or None,
                 horizon=horizon, target_date=target_date,
                 status="active" if uid != ctx.user_id else "proposed",
                 approved_by=ctx.user_id if uid != ctx.user_id else None)
        db.add(g)
        db.flush()
        if uid == ctx.user_id:
            me = db.get(Membership, ctx.membership_id)
            leaders = upline_chain(db, ctx.org_id, me.path)
            if not leaders:
                g.status = "active"  # nobody above to approve (top of the organization)
            for leader in leaders[:1]:
                notify(db, ctx, leader.user_id, "goal_proposed", f"{ctx.user_name} proposed a goal: {title[:100]}",
                       route="/goals?tab=approve")
        else:
            notify(db, ctx, uid, "goal_set", f"{ctx.user_name} set a goal with you: {title[:100]}", route="/goals")
        audit.record(db, ctx, "goal.created", "goal", g.id, for_user=uid)
        _changed(db, ctx)
        return g.id


def update_goal(ctx: TenantContext, goal_id: uuid.UUID, *, progress: int | None = None, status: str | None = None,
                title: str | None = None) -> None:
    with tenant_session(ctx) as db:
        g = db.get(Goal, goal_id)
        if not g:
            raise NotFound("Goal not found")
        is_owner = g.user_id == ctx.user_id
        if progress is not None:
            g.progress = max(0, min(100, int(progress)))
            if g.progress == 100 and g.status == "active":
                g.status = "done"
        if status is not None:
            if status not in STATUSES:
                raise ValidationError("Unknown status")
            if status == "active" and g.status == "proposed" and is_owner:
                raise PermissionDenied("Your leader approves proposed goals.")
            if status == "proposed" and is_owner:
                # Re-proposing after being sent back: goes to the leader again (or straight to
                # active for someone at the top of the organization).
                me = db.get(Membership, ctx.membership_id)
                leaders = upline_chain(db, ctx.org_id, me.path)
                if not leaders:
                    status = "active"
                for leader in leaders[:1]:
                    notify(db, ctx, leader.user_id, "goal_proposed", f"{ctx.user_name} re-proposed a goal: {g.title[:90]}",
                           route="/goals?tab=approve")
            if status == "active" and g.status == "proposed":
                g.approved_by = ctx.user_id
                notify(db, ctx, g.user_id, "goal_approved", f"Goal approved: {g.title[:100]}", route="/goals")
            g.status = status
        if title is not None and title.strip():
            g.title = title.strip()[:200]
        audit.record(db, ctx, "goal.updated", "goal", g.id, progress=progress, status=status)
        _changed(db, ctx)


def pending_approvals(ctx: TenantContext) -> list[tuple[Goal, str]]:
    """Goals proposed by people in the caller's team (admins: the whole organization),
    waiting for a leader to approve. Row-level security limits this to the caller's branch."""
    from ..models import User

    with tenant_session(ctx) as db:
        rows = db.execute(select(Goal, User.name).join(User, User.id == Goal.user_id)
                          .where(Goal.org_id == ctx.org_id, Goal.status == "proposed", Goal.user_id != ctx.user_id)
                          .order_by(Goal.created_at)).all()
        return [(g, name) for g, name in rows]


def decline_goal(ctx: TenantContext, goal_id: uuid.UUID, note: str = "") -> None:
    """A leader sends a proposed goal back with a note; the member can re-propose."""
    with tenant_session(ctx) as db:
        g = db.get(Goal, goal_id)
        if not g:
            raise NotFound("Goal not found")
        if g.user_id == ctx.user_id:
            raise PermissionDenied("You can't decline your own goal.")
        if g.status != "proposed":
            raise ValidationError("Only proposed goals can be declined.")
        g.status = "dropped"
        notify(db, ctx, g.user_id, "goal_declined", f"{ctx.user_name} asked you to rework a goal: {g.title[:80]}",
               body=(note or "").strip() or None, route="/goals")
        audit.record(db, ctx, "goal.declined", "goal", g.id, note=note)
        _changed(db, ctx)


def _changed(db, ctx: TenantContext) -> None:
    from .. import events
    from .notify import after_commit

    after_commit(db, lambda: events.publish(events.org_topic(ctx.org_id), kind="goals"))
