"""Onboarding paths: authoring with versions, enrollment, step completion and sign-off."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from .. import audit, events
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import tenant_session
from ..models import (
    ContentItem,
    Enrollment,
    LearningPath,
    Membership,
    PathStep,
    PathVersion,
    StepProgress,
    User,
)
from .notify import notify
from .orgs import upline_chain

STEP_TYPES = ("read", "task", "acknowledge", "meet")


def _now():
    return datetime.now(timezone.utc)


def _can_edit(ctx: TenantContext, p: LearningPath) -> bool:
    if ctx.is_admin:
        return True
    return ctx.role == "leader" and p.scope_path is not None and (
        p.scope_path == ctx.path or p.scope_path.startswith(ctx.path + "."))


@dataclass
class PathSummary:
    id: uuid.UUID
    title: str
    description: str | None
    published_version: int | None
    has_draft: bool
    scoped: bool
    can_edit: bool
    enrolled_count: int


@dataclass
class StepView:
    id: uuid.UUID
    stage: str
    position: int
    title: str
    instructions_md: str | None
    step_type: str
    content_item_id: uuid.UUID | None
    content_title: str | None
    due_offset_days: int
    requires_signoff: bool
    # Learner-only fields
    status: str = "todo"
    due_on: date | None = None
    locked: bool = False
    overdue: bool = False
    note: str | None = None


# ------------------------------------------------------------ authoring

def create_path(ctx: TenantContext, title: str, description: str = "", team_only: bool = False) -> uuid.UUID:
    ctx.require_author()
    title = (title or "").strip()
    if not title:
        raise ValidationError("Enter a title.")
    with tenant_session(ctx) as db:
        p = LearningPath(org_id=ctx.org_id, title=title[:200], description=(description or "").strip() or None,
                         scope_path=ctx.path if (team_only or not ctx.is_admin) else None, created_by=ctx.user_id)
        db.add(p)
        db.flush()
        v = PathVersion(org_id=ctx.org_id, path_id=p.id, version_no=1, status="draft")
        db.add(v)
        db.flush()
        p.draft_version_id = v.id
        audit.record(db, ctx, "path.created", "path", p.id, title=title)
        return p.id


def list_paths(ctx: TenantContext) -> list[PathSummary]:
    with tenant_session(ctx) as db:
        rows = db.execute(
            select(LearningPath, PathVersion.version_no)
            .outerjoin(PathVersion, PathVersion.id == LearningPath.published_version_id)
            .where(LearningPath.org_id == ctx.org_id, LearningPath.archived.is_(False))
            .order_by(LearningPath.title)
        ).all()
        counts = dict(db.execute(select(Enrollment.path_id, func.count()).where(
            Enrollment.org_id == ctx.org_id, Enrollment.status == "active").group_by(Enrollment.path_id)).all())
        return [PathSummary(p.id, p.title, p.description, vno, p.draft_version_id is not None,
                            p.scope_path is not None, _can_edit(ctx, p), counts.get(p.id, 0)) for p, vno in rows]


def _get_editable(db, ctx, path_id) -> LearningPath:
    p = db.get(LearningPath, path_id)
    if not p:
        raise NotFound("Path not found")
    if not _can_edit(ctx, p):
        raise PermissionDenied("You can't edit this path.")
    return p


def get_path(ctx: TenantContext, path_id: uuid.UUID) -> LearningPath:
    with tenant_session(ctx) as db:
        p = db.get(LearningPath, path_id)
        if not p:
            raise NotFound("Path not found")
        return p


def ensure_draft(ctx: TenantContext, path_id: uuid.UUID) -> uuid.UUID:
    """Returns the draft version, creating one (a copy of the published steps) if needed."""
    with tenant_session(ctx) as db:
        p = _get_editable(db, ctx, path_id)
        if p.draft_version_id:
            return p.draft_version_id
        n = db.scalar(select(func.max(PathVersion.version_no)).where(PathVersion.path_id == p.id)) or 0
        v = PathVersion(org_id=ctx.org_id, path_id=p.id, version_no=n + 1, status="draft")
        db.add(v)
        db.flush()
        if p.published_version_id:
            for s in db.scalars(select(PathStep).where(PathStep.version_id == p.published_version_id)):
                db.add(PathStep(org_id=ctx.org_id, version_id=v.id, stage=s.stage, position=s.position,
                                title=s.title, instructions_md=s.instructions_md, step_type=s.step_type,
                                content_item_id=s.content_item_id, due_offset_days=s.due_offset_days,
                                requires_signoff=s.requires_signoff, step_key=s.step_key))
        p.draft_version_id = v.id
        return v.id


def version_steps(ctx: TenantContext, version_id: uuid.UUID) -> list[StepView]:
    with tenant_session(ctx) as db:
        rows = db.execute(select(PathStep, ContentItem.title).outerjoin(ContentItem, ContentItem.id == PathStep.content_item_id)
                          .where(PathStep.version_id == version_id).order_by(PathStep.position)).all()
        return [_step_view(s, ct) for s, ct in rows]


def _step_view(s: PathStep, content_title) -> StepView:
    return StepView(s.id, s.stage, s.position, s.title, s.instructions_md, s.step_type, s.content_item_id,
                    content_title, s.due_offset_days, s.requires_signoff)


def save_step(ctx: TenantContext, path_id: uuid.UUID, *, title: str, stage: str = "Week 1", instructions_md: str = "",
              step_type: str = "task", content_item_id: uuid.UUID | None = None, due_offset_days: int = 7,
              requires_signoff: bool = False, step_id: uuid.UUID | None = None) -> uuid.UUID:
    title = (title or "").strip()
    if not title:
        raise ValidationError("Enter a step title.")
    if step_type not in STEP_TYPES:
        raise ValidationError("Unknown step type.")
    draft_id = ensure_draft(ctx, path_id)
    with tenant_session(ctx) as db:
        _get_editable(db, ctx, path_id)
        if content_item_id and not db.get(ContentItem, content_item_id):
            raise NotFound("Content not found")
        if step_id:
            s = db.get(PathStep, step_id)
            if not s or s.version_id != draft_id:
                raise ValidationError("You can only edit steps in the draft.")
        else:
            n = db.scalar(select(func.count()).select_from(PathStep).where(PathStep.version_id == draft_id))
            s = PathStep(org_id=ctx.org_id, version_id=draft_id, position=n)
            db.add(s)
        s.title = title[:200]
        s.stage = (stage or "Week 1").strip()[:80]
        s.instructions_md = (instructions_md or "").strip() or None
        s.step_type = step_type
        s.content_item_id = content_item_id
        s.due_offset_days = max(0, int(due_offset_days or 0))
        s.requires_signoff = bool(requires_signoff)
        db.flush()
        return s.id


def delete_step(ctx: TenantContext, path_id: uuid.UUID, step_id: uuid.UUID) -> None:
    draft_id = ensure_draft(ctx, path_id)
    with tenant_session(ctx) as db:
        _get_editable(db, ctx, path_id)
        s = db.get(PathStep, step_id)
        if not s or s.version_id != draft_id:
            raise ValidationError("You can only delete steps in the draft.")
        db.delete(s)
        db.flush()
        for i, st in enumerate(db.scalars(select(PathStep).where(PathStep.version_id == draft_id).order_by(PathStep.position))):
            st.position = i


def move_step(ctx: TenantContext, path_id: uuid.UUID, step_id: uuid.UUID, delta: int) -> None:
    draft_id = ensure_draft(ctx, path_id)
    with tenant_session(ctx) as db:
        _get_editable(db, ctx, path_id)
        steps = list(db.scalars(select(PathStep).where(PathStep.version_id == draft_id).order_by(PathStep.position)))
        idx = next((i for i, s in enumerate(steps) if s.id == step_id), None)
        if idx is None:
            raise NotFound("Step not found")
        j = max(0, min(len(steps) - 1, idx + delta))
        steps.insert(j, steps.pop(idx))
        for i, s in enumerate(steps):
            s.position = i


def publish(ctx: TenantContext, path_id: uuid.UUID) -> int:
    with tenant_session(ctx) as db:
        p = _get_editable(db, ctx, path_id)
        if not p.draft_version_id:
            raise ValidationError("There are no changes to publish.")
        n = db.scalar(select(func.count()).select_from(PathStep).where(PathStep.version_id == p.draft_version_id))
        if not n:
            raise ValidationError("Add at least one step before publishing.")
        v = db.get(PathVersion, p.draft_version_id)
        v.status = "published"
        v.published_at = _now()
        if p.published_version_id:
            db.get(PathVersion, p.published_version_id).status = "retired"
        p.published_version_id = v.id
        p.draft_version_id = None
        audit.record(db, ctx, "path.published", "path", p.id, version=v.version_no)
        return v.version_no


# ------------------------------------------------------------ enrollment

def enroll(ctx: TenantContext, path_id: uuid.UUID, user_ids: list[uuid.UUID], start: date | None = None) -> int:
    """Leaders assign to their downline; admins to anyone. Pins the published version."""
    ctx.require_author()
    start = start or date.today()
    count = 0
    with tenant_session(ctx) as db:
        p = db.get(LearningPath, path_id)
        if not p or not p.published_version_id:
            raise ValidationError("Publish the path before assigning it.")
        for uid in user_ids:
            m = db.scalar(select(Membership).where(Membership.org_id == ctx.org_id, Membership.user_id == uid))
            if not m:
                raise NotFound("Person not found")  # includes people outside the caller's branch (RLS)
            if not ctx.is_admin and not (m.path == ctx.path or m.path.startswith(ctx.path + ".")):
                raise PermissionDenied("You can only assign paths to your own team.")
            if db.scalar(select(Enrollment.id).where(Enrollment.user_id == uid, Enrollment.path_id == p.id,
                                                     Enrollment.status == "active")):
                continue
            e = Enrollment(org_id=ctx.org_id, user_id=uid, path_id=p.id, path_version_id=p.published_version_id,
                           start_date=start, assigned_by=ctx.user_id)
            db.add(e)
            db.flush()
            for s in db.scalars(select(PathStep).where(PathStep.version_id == p.published_version_id)):
                db.add(StepProgress(org_id=ctx.org_id, enrollment_id=e.id, step_id=s.id, user_id=uid))
            notify(db, ctx, uid, "path_assigned", f"New onboarding path: {p.title}", route="/learn")
            audit.record(db, ctx, "path.enrolled", "enrollment", e.id, user=uid, path=p.id)
            count += 1
    return count


def migrate_enrollment(ctx: TenantContext, enrollment_id: uuid.UUID) -> None:
    """Moves an in-progress learner onto the latest published version, keeping completed steps."""
    ctx.require_author()
    with tenant_session(ctx) as db:
        e = db.get(Enrollment, enrollment_id)
        if not e:
            raise NotFound("Enrollment not found")
        p = db.get(LearningPath, e.path_id)
        if not _can_edit(ctx, p):
            raise PermissionDenied("You can't change this enrollment.")
        if e.path_version_id == p.published_version_id:
            return
        old = {s.step_key: sp for sp, s in db.execute(
            select(StepProgress, PathStep).join(PathStep, PathStep.id == StepProgress.step_id)
            .where(StepProgress.enrollment_id == e.id)).all()}
        for sp in old.values():
            db.delete(sp)
        db.flush()
        for s in db.scalars(select(PathStep).where(PathStep.version_id == p.published_version_id)):
            prev = old.get(s.step_key)
            db.add(StepProgress(org_id=ctx.org_id, enrollment_id=e.id, step_id=s.id, user_id=e.user_id,
                                status=prev.status if prev else "todo", note=prev.note if prev else None,
                                completed_at=prev.completed_at if prev else None,
                                signed_off_by=prev.signed_off_by if prev else None,
                                signed_off_at=prev.signed_off_at if prev else None))
        e.path_version_id = p.published_version_id
        audit.record(db, ctx, "enrollment.migrated", "enrollment", e.id)


@dataclass
class EnrollmentView:
    id: uuid.UUID
    path_id: uuid.UUID
    title: str
    version_no: int
    latest_version_no: int | None
    start_date: date
    status: str
    done: int
    total: int
    steps: list[StepView]
    user_id: uuid.UUID | None = None
    user_name: str | None = None

    @property
    def percent(self) -> int:
        return round(100 * self.done / self.total) if self.total else 0


def _enrollment_views(db, ctx, where) -> list[EnrollmentView]:
    rows = db.execute(
        select(Enrollment, LearningPath, PathVersion.version_no, User.name)
        .join(LearningPath, LearningPath.id == Enrollment.path_id)
        .join(PathVersion, PathVersion.id == Enrollment.path_version_id)
        .join(User, User.id == Enrollment.user_id)
        .where(Enrollment.org_id == ctx.org_id, *where)
        .order_by(Enrollment.created_at)
    ).all()
    latest = {}
    out = []
    today = date.today()
    for e, p, vno, uname in rows:
        if p.published_version_id and p.published_version_id not in latest:
            latest[p.published_version_id] = db.scalar(select(PathVersion.version_no).where(PathVersion.id == p.published_version_id))
        steps = []
        prev_done = True
        for sp, s, ct in db.execute(
            select(StepProgress, PathStep, ContentItem.title).join(PathStep, PathStep.id == StepProgress.step_id)
            .outerjoin(ContentItem, ContentItem.id == PathStep.content_item_id)
            .where(StepProgress.enrollment_id == e.id).order_by(PathStep.position)
        ).all():
            v = _step_view(s, ct)
            v.status = sp.status
            v.note = sp.note
            v.due_on = e.start_date + timedelta(days=s.due_offset_days)
            v.locked = not prev_done
            v.overdue = sp.status == "todo" and v.due_on < today
            prev_done = sp.status in ("done", "pending_signoff")
            steps.append(v)
        done = sum(1 for s in steps if s.status == "done")
        out.append(EnrollmentView(e.id, p.id, p.title, vno, latest.get(p.published_version_id), e.start_date, e.status,
                                  done, len(steps), steps, e.user_id, uname))
    return out


def my_enrollments(ctx: TenantContext) -> list[EnrollmentView]:
    with tenant_session(ctx) as db:
        return _enrollment_views(db, ctx, [Enrollment.user_id == ctx.user_id, Enrollment.status != "cancelled"])


def person_enrollments(ctx: TenantContext, user_id: uuid.UUID) -> list[EnrollmentView]:
    with tenant_session(ctx) as db:  # RLS limits this to the caller's branch
        return _enrollment_views(db, ctx, [Enrollment.user_id == user_id, Enrollment.status != "cancelled"])


def team_enrollments(ctx: TenantContext) -> list[EnrollmentView]:
    with tenant_session(ctx) as db:
        return _enrollment_views(db, ctx, [Enrollment.user_id != ctx.user_id, Enrollment.status == "active"])


def get_step(ctx: TenantContext, step_id: uuid.UUID) -> tuple[StepView, EnrollmentView | None]:
    """A step as seen by the current learner (with their progress, if enrolled)."""
    for e in my_enrollments(ctx):
        for s in e.steps:
            if s.id == step_id:
                return s, e
    with tenant_session(ctx) as db:
        row = db.execute(select(PathStep, ContentItem.title).outerjoin(ContentItem, ContentItem.id == PathStep.content_item_id)
                         .where(PathStep.id == step_id)).first()
        if not row:
            raise NotFound("Step not found")
        return _step_view(*row), None


def complete_step(ctx: TenantContext, step_id: uuid.UUID, note: str = "") -> str:
    with tenant_session(ctx) as db:
        row = db.execute(select(StepProgress, PathStep, Enrollment).join(PathStep, PathStep.id == StepProgress.step_id)
                         .join(Enrollment, Enrollment.id == StepProgress.enrollment_id)
                         .where(StepProgress.step_id == step_id, StepProgress.user_id == ctx.user_id,
                                Enrollment.status == "active")).first()
        if not row:
            raise NotFound("You're not enrolled in this step.")
        sp, step, e = row
        if sp.status != "todo":
            return sp.status
        earlier = db.scalars(select(StepProgress.status).join(PathStep, PathStep.id == StepProgress.step_id)
                             .where(StepProgress.enrollment_id == e.id, PathStep.position < step.position)).all()
        if any(s == "todo" for s in earlier):
            raise ValidationError("Finish the earlier steps first.")
        sp.note = (note or "").strip() or None
        sp.completed_at = _now()
        if step.requires_signoff:
            sp.status = "pending_signoff"
            m = db.get(Membership, ctx.membership_id)
            for leader in upline_chain(db, ctx.org_id, m.path)[:1]:
                notify(db, ctx, leader.user_id, "signoff_needed", f"{ctx.user_name} needs sign-off: {step.title}",
                       route="/team-progress")
        else:
            sp.status = "done"
        _maybe_complete(db, e)
        audit.record(db, ctx, "step.completed", "step", step.id, status=sp.status)
        status = sp.status
    events.publish(events.org_topic(ctx.org_id), kind="progress", user_id=str(ctx.user_id))
    return status


def _maybe_complete(db, e: Enrollment) -> None:
    statuses = db.scalars(select(StepProgress.status).where(StepProgress.enrollment_id == e.id)).all()
    if statuses and all(s == "done" for s in statuses):
        e.status = "completed"
        e.completed_at = _now()


def pending_signoffs(ctx: TenantContext) -> list[tuple[StepProgress, PathStep, str, str]]:
    if not ctx.is_author:
        return []
    with tenant_session(ctx) as db:
        rows = db.execute(
            select(StepProgress, PathStep, User.name, LearningPath.title)
            .join(PathStep, PathStep.id == StepProgress.step_id)
            .join(Enrollment, Enrollment.id == StepProgress.enrollment_id)
            .join(LearningPath, LearningPath.id == Enrollment.path_id)
            .join(User, User.id == StepProgress.user_id)
            .where(StepProgress.org_id == ctx.org_id, StepProgress.status == "pending_signoff",
                   StepProgress.user_id != ctx.user_id)
            .order_by(StepProgress.completed_at)
        ).all()
        return [tuple(r) for r in rows]


def sign_off(ctx: TenantContext, progress_id: uuid.UUID, approve: bool = True, comment: str = "") -> None:
    ctx.require_author()
    with tenant_session(ctx) as db:
        sp = db.get(StepProgress, progress_id)  # RLS: only visible within the caller's branch
        if not sp or sp.status != "pending_signoff":
            raise NotFound("Nothing to sign off.")
        if sp.user_id == ctx.user_id:
            raise PermissionDenied("You can't sign off your own step.")
        step = db.get(PathStep, sp.step_id)
        if approve:
            sp.status = "done"
            sp.signed_off_by = ctx.user_id
            sp.signed_off_at = _now()
            _maybe_complete(db, db.get(Enrollment, sp.enrollment_id))
            notify(db, ctx, sp.user_id, "signed_off", f"Signed off: {step.title}", body=comment or None, route="/learn")
        else:
            sp.status = "todo"
            notify(db, ctx, sp.user_id, "signoff_returned", f"Needs another look: {step.title}", body=comment or None,
                   route=f"/learn/step/{step.id}")
        audit.record(db, ctx, "step.signed_off" if approve else "step.returned", "step_progress", sp.id, comment=comment)
    events.publish(events.org_topic(ctx.org_id), kind="progress", user_id=str(sp.user_id))
