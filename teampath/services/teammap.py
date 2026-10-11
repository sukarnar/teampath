"""Team map: the caller's downline as nested bubbles, plus private focus pins,
notes, layouts and opportunity sharing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import circlify
from sqlalchemy import func, select, text

from .. import audit
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import tenant_session
from ..models import (
    Enrollment,
    FocusPin,
    Goal,
    MapLayout,
    Membership,
    Momentum,
    OpportunityShare,
    PersonNote,
    StepProgress,
    Thread,
    User,
)
from .notify import notify

STATUS_LABELS = {"active": "Active", "slowing": "Slowing", "inactive": "Inactive", "new": "New"}
MAX_PINS = 5


@dataclass
class MapNode:
    user_id: uuid.UUID
    membership_id: uuid.UUID
    parent_membership_id: uuid.UUID | None
    name: str
    role: str
    title: str | None
    team_name: str | None
    path: str
    depth: int  # relative to the map root
    team_size: int = 0  # everyone below this person
    week_activity: int = 0
    momentum: str = "new"
    last_activity_on: date | None = None
    onboarding_percent: int | None = None
    overdue_steps: int = 0
    pending_signoffs: int = 0
    open_questions: int = 0
    proposed_goals: int = 0
    pinned: bool = False
    tags: list[str] = field(default_factory=list)
    children: list["MapNode"] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.overdue_steps or self.pending_signoffs or self.open_questions or self.proposed_goals)

    @property
    def days_idle(self) -> int | None:
        return (date.today() - self.last_activity_on).days if self.last_activity_on else None


def build_tree(ctx: TenantContext, root_user_id: uuid.UUID | None = None) -> MapNode | None:
    """Returns the hierarchy under `root_user_id` (default: the caller), with stats.
    Leaders only ever get their own branch — the queries run under RLS as well."""
    with tenant_session(ctx) as db:
        root = db.scalar(select(Membership).where(Membership.org_id == ctx.org_id,
                                                  Membership.user_id == (root_user_id or ctx.user_id)))
        if not root:
            raise NotFound("Person not found")
        if not ctx.is_admin and not ctx.is_ancestor_of(root.path):
            raise PermissionDenied("You can only view your own team.")
        rows = db.execute(
            select(Membership, User.name).join(User, User.id == Membership.user_id)
            .where(Membership.org_id == ctx.org_id, Membership.status == "active",
                   (Membership.role != "assistant") | (Membership.id == root.id),
                   text("memberships.path <@ cast(:p as ltree)").bindparams(p=root.path))
            .order_by(Membership.path)
        ).all()
        uids = [m.user_id for m, _ in rows]
        mom = {m.user_id: m for m in db.scalars(select(Momentum).where(Momentum.org_id == ctx.org_id,
                                                                        Momentum.user_id.in_(uids)))}
        # Onboarding progress per person: done steps / all steps of active enrollments.
        prog = db.execute(
            select(StepProgress.user_id,
                   func.count().filter(StepProgress.status == "done"), func.count(),
                   func.count().filter(StepProgress.status == "pending_signoff"))
            .join(Enrollment, Enrollment.id == StepProgress.enrollment_id)
            .where(Enrollment.status == "active", StepProgress.user_id.in_(uids))
            .group_by(StepProgress.user_id)
        ).all()
        overdue = dict(db.execute(text(
            "select sp.user_id, count(*) from step_progress sp join enrollments e on e.id = sp.enrollment_id "
            "join path_steps s on s.id = sp.step_id where e.status = 'active' and sp.status = 'todo' "
            "and e.start_date + s.due_offset_days < current_date and sp.user_id = any(:u) group by sp.user_id"
        ), {"u": uids}).all())
        questions = dict(db.execute(select(Thread.author_id, func.count()).where(
            Thread.author_id.in_(uids), Thread.is_question.is_(True), Thread.status == "open")
            .group_by(Thread.author_id)).all())
        proposed = dict(db.execute(select(Goal.user_id, func.count()).where(
            Goal.user_id.in_(uids), Goal.status == "proposed").group_by(Goal.user_id)).all())
        pins = set(db.scalars(select(FocusPin.target_user_id).where(FocusPin.owner_id == ctx.user_id,
                                                                     FocusPin.org_id == ctx.org_id)))
        tags = dict(db.execute(select(PersonNote.subject_user_id, PersonNote.tags).where(
            PersonNote.author_id == ctx.user_id, PersonNote.org_id == ctx.org_id)).all())

    base_depth = root.path.count(".")
    nodes: dict[uuid.UUID, MapNode] = {}
    for m, name in rows:
        mm = mom.get(m.user_id)
        nodes[m.id] = MapNode(
            m.user_id, m.id, m.parent_id, name, m.role, m.title, m.team_name, m.path, m.path.count(".") - base_depth,
            week_activity=mm.week_total if mm else 0, momentum=mm.status if mm else "new",
            last_activity_on=mm.last_activity_on if mm else None,
            overdue_steps=int(overdue.get(m.user_id, 0)), open_questions=int(questions.get(m.user_id, 0)),
            proposed_goals=int(proposed.get(m.user_id, 0)) if m.user_id != ctx.user_id else 0,
            pinned=m.user_id in pins, tags=list(tags.get(m.user_id) or []),
        )
    by_user = {n.user_id: n for n in nodes.values()}
    for uid, done, total, pending in prog:
        n = by_user.get(uid)
        if n:
            n.onboarding_percent = round(100 * done / total) if total else None
            n.pending_signoffs = int(pending)
    root_node = nodes[root.id]
    for n in nodes.values():
        if n is not root_node and n.parent_membership_id in nodes:
            nodes[n.parent_membership_id].children.append(n)

    def size(n: MapNode) -> int:
        n.team_size = sum(1 + size(c) for c in n.children)
        return n.team_size

    size(root_node)
    return root_node


@dataclass
class Bubble:
    node: MapNode
    x: float  # 0..1, relative to the canvas
    y: float
    r: float
    level: int


def pack(root: MapNode, size_by: str = "team", max_depth: int = 2) -> list[Bubble]:
    """Circle-packing layout. Shows `max_depth` levels below the root; deeper
    levels are reached by drilling into a bubble."""

    def weight(n: MapNode) -> float:
        if size_by == "activity":
            return max(1.0, float(n.week_activity))
        return 1.0 + n.team_size

    def to_data(n: MapNode, depth: int) -> dict:
        d = {"id": str(n.membership_id), "datum": weight(n)}
        kids = [to_data(c, depth + 1) for c in n.children] if depth < max_depth else []
        if kids:
            # The parent bubble encloses its team; a small "self" circle keeps
            # the leader visible inside their own team.
            kids.append({"id": f"self:{n.membership_id}", "datum": max(1.0, weight(n) / (len(kids) + 2))})
            d["children"] = kids
            d["datum"] = sum(k["datum"] for k in kids)
        return d

    index = {}

    def walk(n: MapNode):
        index[str(n.membership_id)] = n
        for c in n.children:
            walk(c)

    walk(root)
    if not root.children:
        return [Bubble(root, 0.5, 0.5, 0.25, 1)]
    data = [to_data(c, 1) for c in root.children]
    circles = circlify.circlify(data, show_enclosure=False, target_enclosure=circlify.Circle(x=0, y=0, r=1))
    out = []
    for c in circles:
        key = c.ex["id"] if c.ex else None
        if not key or key.startswith("self:"):
            continue
        out.append(Bubble(index[key], (c.x + 1) / 2, (1 - c.y) / 2, c.r / 2, c.level))
    out.sort(key=lambda b: b.level)
    return out


def flatten(root: MapNode) -> list[MapNode]:
    out = []

    def walk(n):
        for c in n.children:
            out.append(c)
            walk(c)

    walk(root)
    return out


# ------------------------------------------------------------ private: pins, notes, layout

def _check_visible(db, ctx, user_id) -> Membership:
    m = db.scalar(select(Membership).where(Membership.org_id == ctx.org_id, Membership.user_id == user_id))
    if not m or not (ctx.is_admin or m.path.startswith(ctx.path + ".")):
        raise PermissionDenied("That person is not in your team.")
    return m


def toggle_pin(ctx: TenantContext, user_id: uuid.UUID, reason: str = "") -> bool:
    with tenant_session(ctx) as db:
        _check_visible(db, ctx, user_id)
        pin = db.scalar(select(FocusPin).where(FocusPin.owner_id == ctx.user_id, FocusPin.org_id == ctx.org_id,
                                               FocusPin.target_user_id == user_id))
        if pin:
            db.delete(pin)
            return False
        n = len(db.scalars(select(FocusPin.id).where(FocusPin.owner_id == ctx.user_id, FocusPin.org_id == ctx.org_id)).all())
        if n >= MAX_PINS:
            raise ValidationError(f"You can focus on up to {MAX_PINS} people. Unpin someone first.")
        db.add(FocusPin(org_id=ctx.org_id, owner_id=ctx.user_id, target_user_id=user_id, reason=(reason or "")[:200] or None))
        return True


def get_note(ctx: TenantContext, user_id: uuid.UUID) -> PersonNote | None:
    with tenant_session(ctx) as db:
        return db.scalar(select(PersonNote).where(PersonNote.author_id == ctx.user_id, PersonNote.org_id == ctx.org_id,
                                                  PersonNote.subject_user_id == user_id))


def save_note(ctx: TenantContext, user_id: uuid.UUID, tags: list[str], note: str) -> None:
    with tenant_session(ctx) as db:
        _check_visible(db, ctx, user_id)
        n = db.scalar(select(PersonNote).where(PersonNote.author_id == ctx.user_id, PersonNote.org_id == ctx.org_id,
                                               PersonNote.subject_user_id == user_id))
        if not n:
            n = PersonNote(org_id=ctx.org_id, author_id=ctx.user_id, subject_user_id=user_id)
            db.add(n)
        n.tags = sorted({t.strip().lower()[:40] for t in tags if t.strip()})[:10]
        n.note = (note or "").strip()[:2000] or None
        n.updated_at = datetime.now(timezone.utc)


def get_layout(ctx: TenantContext) -> dict:
    with tenant_session(ctx) as db:
        lay = db.get(MapLayout, (ctx.org_id, ctx.user_id))
        return dict(lay.layout) if lay else {}


def save_layout(ctx: TenantContext, layout: dict) -> None:
    positions = {k: [round(float(v[0]), 4), round(float(v[1]), 4)] for k, v in (layout.get("positions") or {}).items()}
    groups = {str(k)[:40]: [str(x) for x in v][:200] for k, v in (layout.get("groups") or {}).items()}
    with tenant_session(ctx) as db:
        lay = db.get(MapLayout, (ctx.org_id, ctx.user_id))
        if not lay:
            lay = MapLayout(org_id=ctx.org_id, owner_id=ctx.user_id)
            db.add(lay)
        lay.layout = {"positions": positions, "groups": groups}


@dataclass
class FocusItem:
    node: MapNode
    suggestion: str


def suggestion_for(n: MapNode) -> str:
    if n.pending_signoffs:
        return f"Sign off {n.pending_signoffs} step(s) waiting for you"
    if n.open_questions:
        return f"Answer {n.open_questions} open question(s)"
    if n.proposed_goals:
        return f"Review {n.proposed_goals} proposed goal(s)"
    if n.momentum == "inactive":
        idle = n.days_idle
        return f"Check in — no activity for {idle} days" if idle else "Check in — no activity logged yet"
    if n.momentum == "slowing":
        return "Momentum is slowing — offer help this week"
    if n.overdue_steps:
        return f"{n.overdue_steps} onboarding step(s) overdue"
    if n.momentum == "new":
        return "New — make sure they're set up and know their first steps"
    return "On track — recognise their progress"


def my_focus(ctx: TenantContext) -> list[FocusItem]:
    root = build_tree(ctx)
    pinned = [n for n in flatten(root) if n.pinned]
    return [FocusItem(n, suggestion_for(n)) for n in pinned]


def attention_list(ctx: TenantContext, limit: int = 8) -> list[FocusItem]:
    """Unpinned people in the caller's branch who most need attention."""
    root = build_tree(ctx)
    rank = {"inactive": 0, "slowing": 1, "new": 2, "active": 3}
    people = [n for n in flatten(root) if not n.pinned and (n.needs_attention or n.momentum in ("inactive", "slowing"))]
    people.sort(key=lambda n: (-(n.pending_signoffs + n.open_questions + n.proposed_goals), rank.get(n.momentum, 3), n.depth))
    return [FocusItem(n, suggestion_for(n)) for n in people[:limit]]


# ------------------------------------------------------------ opportunity sharing (no client PII)

def share_opportunity(ctx: TenantContext, to_user_id: uuid.UUID, title: str) -> uuid.UUID:
    title = (title or "").strip()
    if not title:
        raise ValidationError("Give the opportunity a short label (no client details).")
    with tenant_session(ctx) as db:
        if to_user_id == ctx.user_id:
            raise ValidationError("Choose a teammate.")
        m = db.scalar(select(Membership).where(Membership.org_id == ctx.org_id, Membership.user_id == to_user_id))
        if not m:
            raise NotFound("Teammate not found")
        o = OpportunityShare(org_id=ctx.org_id, from_user_id=ctx.user_id, to_user_id=to_user_id, title=title[:120])
        db.add(o)
        db.flush()
        notify(db, ctx, to_user_id, "opportunity", f"{ctx.user_name} shared an opportunity: {title[:80]}",
               route="/opportunities")
        audit.record(db, ctx, "opportunity.shared", "opportunity", o.id, to=to_user_id)
        return o.id


def update_opportunity(ctx: TenantContext, opp_id: uuid.UUID, status: str, note: str = "") -> None:
    if status not in ("accepted", "won", "lost"):
        raise ValidationError("Unknown status")
    with tenant_session(ctx) as db:
        o = db.get(OpportunityShare, opp_id)
        if not o:
            raise NotFound("Not found")
        if ctx.user_id not in (o.to_user_id, o.from_user_id):
            raise PermissionDenied("Only the people involved can update this.")
        o.status = status
        o.outcome_note = (note or "").strip()[:300] or None
        other = o.from_user_id if ctx.user_id == o.to_user_id else o.to_user_id
        notify(db, ctx, other, "opportunity", f"Opportunity '{o.title[:60]}' marked {status}", route="/opportunities")


def list_opportunities(ctx: TenantContext) -> list[tuple[OpportunityShare, str, str]]:
    with tenant_session(ctx) as db:
        rows = list(db.scalars(select(OpportunityShare).where(OpportunityShare.org_id == ctx.org_id)
                               .where((OpportunityShare.from_user_id == ctx.user_id) | (OpportunityShare.to_user_id == ctx.user_id))
                               .order_by(OpportunityShare.created_at.desc()).limit(100)))
        ids = {r.from_user_id for r in rows} | {r.to_user_id for r in rows}
        names = dict(db.execute(select(User.id, User.name).where(User.id.in_(ids))).all()) if ids else {}
        return [(r, names.get(r.from_user_id, ""), names.get(r.to_user_id, "")) for r in rows]


def teammates(ctx: TenantContext) -> list[tuple[uuid.UUID, str]]:
    """People the caller can share with: their upline, downline and direct peers' visible set."""
    from .orgs import list_people

    return [(p.user_id, p.name) for p in list_people(ctx, include_upline=True) if p.user_id != ctx.user_id]
