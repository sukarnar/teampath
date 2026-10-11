"""Tasks and delegation: agents hand work to their assistants and track it.

* An assistant is linked to one or more agents (`assistant_links`). Inviting someone
  as an assistant links them to the inviting agent; agents can also link someone in
  their own team, and admins can link anyone.
* A task belongs to the agent who created it and is done by the agent or one of their
  assistants. Only those two people can see it (row-level security) — not leaders,
  not admins. Details, notes and comments are encrypted at rest.
* A task can be about one of the agent's prospects. While it's open, the assistant can
  see that prospect's contact details and log calls on it; access ends automatically
  when the task is done or cancelled. The agent's private notes stay private.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import func, or_, select, update

from .. import audit, events
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import system_session, tenant_session
from ..models import AssistantLink, Membership, Prospect, Task, TaskComment, User
from ..security import decrypt, encrypt
from .notify import after_commit, notify

STATUSES = {"open": "To do", "in_progress": "In progress", "done": "Done", "cancelled": "Cancelled"}
OPEN = ("open", "in_progress")
PRIORITIES = ("normal", "high")


def _now():
    return datetime.now(timezone.utc)


def _enc(v: str | None) -> str | None:
    v = (v or "").strip()
    return encrypt(v) if v else None


def _dec(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return decrypt(v)
    except Exception:
        return None


def _changed(db, ctx: TenantContext, task_id) -> None:
    after_commit(db, lambda: events.publish(events.org_topic(ctx.org_id), kind="tasks", task_id=str(task_id)))


# ------------------------------------------------------------ assistants

@dataclass
class Helper:
    user_id: uuid.UUID
    name: str
    email: str
    role: str
    open_tasks: int = 0


def my_assistants(ctx: TenantContext) -> list[Helper]:
    with tenant_session(ctx) as db:
        rows = db.execute(
            select(User.id, User.name, User.email, Membership.role)
            .join(AssistantLink, AssistantLink.assistant_id == User.id)
            .join(Membership, (Membership.user_id == User.id) & (Membership.org_id == ctx.org_id))
            .where(AssistantLink.org_id == ctx.org_id, AssistantLink.agent_id == ctx.user_id,
                   AssistantLink.active.is_(True), Membership.status == "active")
            .order_by(User.name)).all()
        counts = dict(db.execute(select(Task.assignee_id, func.count()).where(
            Task.org_id == ctx.org_id, Task.owner_id == ctx.user_id, Task.status.in_(OPEN))
            .group_by(Task.assignee_id)).all())
        return [Helper(u, n, e, r, int(counts.get(u, 0))) for u, n, e, r in rows]


def my_agents(ctx: TenantContext) -> list[Helper]:
    """Agents the current user assists."""
    with tenant_session(ctx) as db:
        rows = db.execute(
            select(User.id, User.name, User.email, Membership.role)
            .join(AssistantLink, AssistantLink.agent_id == User.id)
            .join(Membership, (Membership.user_id == User.id) & (Membership.org_id == ctx.org_id))
            .where(AssistantLink.org_id == ctx.org_id, AssistantLink.assistant_id == ctx.user_id,
                   AssistantLink.active.is_(True), Membership.status == "active")
            .order_by(User.name)).all()
        return [Helper(u, n, e, r) for u, n, e, r in rows]


def link_assistant(ctx: TenantContext, assistant_user_id: uuid.UUID, agent_user_id: uuid.UUID | None = None) -> None:
    """Make someone an assistant of an agent (default: of the caller).
    Agents may pick someone in their own team; admins may link anyone."""
    agent_id = agent_user_id or ctx.user_id
    if agent_id != ctx.user_id and not ctx.is_admin:
        raise PermissionDenied("Only admins can assign assistants to other agents.")
    if assistant_user_id == agent_id:
        raise ValidationError("Choose someone else as the assistant.")
    if ctx.is_assistant and agent_id == ctx.user_id:
        raise PermissionDenied("Assistants can't have assistants.")
    with tenant_session(ctx) as db:
        m = db.scalar(select(Membership).where(Membership.org_id == ctx.org_id,
                                               Membership.user_id == assistant_user_id,
                                               Membership.status == "active"))
        if not m:
            raise NotFound("Person not found")
        if not ctx.is_admin and not m.path.startswith(ctx.path + "."):
            raise PermissionDenied("You can choose someone in your own team, or invite a new assistant.")
        link = db.scalar(select(AssistantLink).where(AssistantLink.org_id == ctx.org_id,
                                                     AssistantLink.agent_id == agent_id,
                                                     AssistantLink.assistant_id == assistant_user_id))
        if link:
            link.active = True
        else:
            db.add(AssistantLink(org_id=ctx.org_id, agent_id=agent_id, assistant_id=assistant_user_id))
        agent_name = db.scalar(select(User.name).where(User.id == agent_id))
        notify(db, ctx, assistant_user_id, "assistant_linked", f"You're now an assistant to {agent_name}",
               body="They can delegate tasks to you. You'll find them under Tasks.", route="/tasks")
        audit.record(db, ctx, "assistant.linked", "user", assistant_user_id, agent=agent_id)


def unlink_assistant(ctx: TenantContext, assistant_user_id: uuid.UUID, agent_user_id: uuid.UUID | None = None) -> int:
    """End an assistant relationship. Open tasks delegated to them go back to the agent.
    Returns how many tasks were handed back."""
    agent_id = agent_user_id or ctx.user_id
    if ctx.user_id not in (agent_id, assistant_user_id) and not ctx.is_admin:
        raise PermissionDenied("You can't change that.")
    with tenant_session(ctx) as db:
        link = db.scalar(select(AssistantLink).where(AssistantLink.org_id == ctx.org_id,
                                                     AssistantLink.agent_id == agent_id,
                                                     AssistantLink.assistant_id == assistant_user_id,
                                                     AssistantLink.active.is_(True)))
        if not link:
            raise NotFound("Not linked")
        link.active = False
        audit.record(db, ctx, "assistant.unlinked", "user", assistant_user_id, agent=agent_id)
    return _hand_back(ctx.org_id, agent_id, assistant_user_id, "Assistant link ended")


def _hand_back(org_id, agent_id, assistant_id, reason: str) -> int:
    """Open tasks owned by `agent_id` (or anyone, if agent_id is None) and assigned to
    `assistant_id` go back to their owners."""
    with system_session("tasks: hand delegated tasks back to their owner") as db:
        q = select(Task).where(Task.org_id == org_id, Task.assignee_id == assistant_id, Task.status.in_(OPEN),
                               Task.owner_id != assistant_id)
        if agent_id:
            q = q.where(Task.owner_id == agent_id)
        moved = list(db.scalars(q))
        for t in moved:
            t.assignee_id, t.status, t.updated_at = t.owner_id, "open", _now()
            notify(db, None, t.owner_id, "task_returned", f"Task back with you: {t.title[:90]}", body=reason,
                   route=f"/tasks/{t.id}", org_id=org_id)
        return len(moved)


def release_user_tasks(org_id, user_id) -> None:
    """Called when someone leaves the organization."""
    _hand_back(org_id, None, user_id, "The person it was delegated to left the organization")
    with system_session("tasks: deactivate assistant links of a released member") as db:
        db.execute(update(AssistantLink).where(AssistantLink.org_id == org_id,
                                               or_(AssistantLink.agent_id == user_id,
                                                   AssistantLink.assistant_id == user_id))
                   .values(active=False))


# ------------------------------------------------------------ tasks

def _assert_can_assign(db, ctx: TenantContext, assignee_id: uuid.UUID) -> None:
    if assignee_id == ctx.user_id:
        return
    ok = db.scalar(select(AssistantLink.id).where(AssistantLink.org_id == ctx.org_id,
                                                  AssistantLink.agent_id == ctx.user_id,
                                                  AssistantLink.assistant_id == assignee_id,
                                                  AssistantLink.active.is_(True)))
    if not ok:
        raise PermissionDenied("You can delegate only to your own assistants.")


def create(ctx: TenantContext, *, title: str, details: str = "", assignee_id: uuid.UUID | None = None,
           due_on: date | None = None, due_time: str | None = None, priority: str = "normal",
           prospect_id: uuid.UUID | None = None) -> uuid.UUID:
    from .prospects import _clean_time

    title = (title or "").strip()
    if not title:
        raise ValidationError("Describe the task.")
    if priority not in PRIORITIES:
        raise ValidationError("Unknown priority.")
    if due_on and due_on < ctx.today():
        raise ValidationError("The due date can't be in the past.")
    assignee_id = assignee_id or ctx.user_id
    with tenant_session(ctx) as db:
        _assert_can_assign(db, ctx, assignee_id)
        if prospect_id:
            p = db.get(Prospect, prospect_id)
            if not p or p.owner_id != ctx.user_id:
                raise NotFound("Prospect not found")
        t = Task(org_id=ctx.org_id, owner_id=ctx.user_id, assignee_id=assignee_id, title=title[:200],
                 details_enc=_enc(details), due_on=due_on, due_time=_clean_time(due_time), priority=priority,
                 prospect_id=prospect_id)
        db.add(t)
        db.flush()
        if assignee_id != ctx.user_id:
            when = f" · due {due_on:%a %b %d}" if due_on else ""
            notify(db, ctx, assignee_id, "task_assigned", f"{ctx.user_name} delegated: {title[:100]}{when}",
                   route=f"/tasks/{t.id}")
        audit.record(db, ctx, "task.created", "task", t.id, delegated=assignee_id != ctx.user_id,
                     with_prospect=bool(prospect_id))
        _changed(db, ctx, t.id)
        return t.id


def _get(db, ctx, task_id) -> Task:
    t = db.get(Task, task_id)  # RLS: only the owner and the assignee can see it
    if not t:
        raise NotFound("Task not found")
    return t


def edit(ctx: TenantContext, task_id: uuid.UUID, **f) -> None:
    """The owner changes the task (including who it's delegated to)."""
    from .prospects import _clean_time

    with tenant_session(ctx) as db:
        t = _get(db, ctx, task_id)
        if t.owner_id != ctx.user_id:
            raise PermissionDenied("Only the person who created the task can change it.")
        if "title" in f:
            if not (f["title"] or "").strip():
                raise ValidationError("Describe the task.")
            t.title = f["title"].strip()[:200]
        if "details" in f:
            t.details_enc = _enc(f["details"])
        if "due_on" in f:
            if f["due_on"] and f["due_on"] < ctx.today() and f["due_on"] != t.due_on:
                raise ValidationError("The due date can't be in the past.")
            t.due_on = f["due_on"]
        if "due_time" in f:
            t.due_time = _clean_time(f["due_time"])
        if "priority" in f:
            if f["priority"] not in PRIORITIES:
                raise ValidationError("Unknown priority.")
            t.priority = f["priority"]
        if "assignee_id" in f and f["assignee_id"] and f["assignee_id"] != t.assignee_id:
            new = f["assignee_id"]
            _assert_can_assign(db, ctx, new)
            old = t.assignee_id
            t.assignee_id = new
            if t.status == "in_progress":
                t.status = "open"
            if new != ctx.user_id:
                notify(db, ctx, new, "task_assigned", f"{ctx.user_name} delegated: {t.title[:100]}",
                       route=f"/tasks/{t.id}")
            if old != ctx.user_id:
                notify(db, ctx, old, "task_reassigned", f"Task moved to someone else: {t.title[:90]}", route="/tasks")
        t.updated_at = _now()
        audit.record(db, ctx, "task.edited", "task", t.id, fields=sorted(f))
        _changed(db, ctx, t.id)


def set_status(ctx: TenantContext, task_id: uuid.UUID, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise ValidationError("Unknown status.")
    with tenant_session(ctx) as db:
        t = _get(db, ctx, task_id)
        if status == "cancelled" and t.owner_id != ctx.user_id:
            raise PermissionDenied("Only the person who created the task can cancel it.")
        old = t.status
        t.status, t.updated_at = status, _now()
        if status == "done":
            t.completed_at = _now()
            t.completion_note_enc = _enc(note)
        elif status in OPEN:
            t.completed_at = None
        other = t.owner_id if ctx.user_id == t.assignee_id else t.assignee_id
        if other != ctx.user_id and old != status:
            label = {"done": "Done", "in_progress": "Started", "cancelled": "Cancelled", "open": "Reopened"}[status]
            notify(db, ctx, other, f"task_{status}", f"{label}: {t.title[:100]}",
                   body=(note or "").strip()[:300] or None, route=f"/tasks/{t.id}")
        audit.record(db, ctx, f"task.{status}", "task", t.id)
        _changed(db, ctx, t.id)


def delete(ctx: TenantContext, task_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        t = _get(db, ctx, task_id)
        if t.owner_id != ctx.user_id:
            raise PermissionDenied("Only the person who created the task can delete it.")
        if t.assignee_id != ctx.user_id and t.status in OPEN:
            notify(db, ctx, t.assignee_id, "task_cancelled", f"Cancelled: {t.title[:100]}", route="/tasks")
        db.delete(t)
        audit.record(db, ctx, "task.deleted", "task", task_id)
        _changed(db, ctx, task_id)


def comment(ctx: TenantContext, task_id: uuid.UUID, body: str) -> None:
    body = (body or "").strip()
    if not body:
        raise ValidationError("Write a comment.")
    with tenant_session(ctx) as db:
        t = _get(db, ctx, task_id)
        db.add(TaskComment(org_id=ctx.org_id, task_id=t.id, author_id=ctx.user_id, body_enc=encrypt(body[:4000])))
        t.updated_at = _now()
        other = t.owner_id if ctx.user_id == t.assignee_id else t.assignee_id
        if other != ctx.user_id:
            notify(db, ctx, other, "task_comment", f"{ctx.user_name} commented: {t.title[:90]}",
                   body=body[:140], route=f"/tasks/{t.id}")
        _changed(db, ctx, t.id)


# ------------------------------------------------------------ reading

@dataclass
class TaskRow:
    id: uuid.UUID
    title: str
    status: str
    priority: str
    due_on: date | None
    due_time: str | None
    owner_id: uuid.UUID
    owner_name: str
    assignee_id: uuid.UUID
    assignee_name: str
    prospect_id: uuid.UUID | None
    prospect_name: str | None
    updated_at: datetime
    completed_at: datetime | None
    comments: int = 0

    @property
    def delegated(self) -> bool:
        return self.owner_id != self.assignee_id

    @property
    def is_open(self) -> bool:
        return self.status in OPEN


@dataclass
class CommentView:
    author_name: str
    author_id: uuid.UUID
    body: str
    created_at: datetime


@dataclass
class TaskDetail(TaskRow):
    details: str | None = None
    completion_note: str | None = None
    created_at: datetime | None = None
    thread: list[CommentView] = field(default_factory=list)


def _rows(db, ctx, where, limit=300) -> list[TaskRow]:
    tasks = list(db.scalars(select(Task).where(Task.org_id == ctx.org_id, *where)
                            .order_by(Task.due_on.asc().nulls_last(), Task.due_time.asc().nulls_last(),
                                      Task.priority.desc(), Task.created_at).limit(limit)))
    if not tasks:
        return []
    ids = {t.owner_id for t in tasks} | {t.assignee_id for t in tasks}
    names = dict(db.execute(select(User.id, User.name).where(User.id.in_(ids))).all())
    pids = {t.prospect_id for t in tasks if t.prospect_id}
    # Prospect names: visible to the owner, and to the assignee while the task is open (RLS).
    pnames = dict(db.execute(select(Prospect.id, Prospect.name).where(Prospect.id.in_(pids))).all()) if pids else {}
    counts = dict(db.execute(select(TaskComment.task_id, func.count()).where(
        TaskComment.task_id.in_([t.id for t in tasks])).group_by(TaskComment.task_id)).all())
    return [TaskRow(t.id, t.title, t.status, t.priority, t.due_on, t.due_time, t.owner_id, names.get(t.owner_id, ""),
                    t.assignee_id, names.get(t.assignee_id, ""), t.prospect_id, pnames.get(t.prospect_id),
                    t.updated_at, t.completed_at, int(counts.get(t.id, 0))) for t in tasks]


def assigned_to_me(ctx: TenantContext, include_done: bool = False) -> list[TaskRow]:
    with tenant_session(ctx) as db:
        where = [Task.assignee_id == ctx.user_id]
        if not include_done:
            where.append(Task.status.in_(OPEN))
        return _rows(db, ctx, where)


def delegated_by_me(ctx: TenantContext, include_done: bool = False, assignee_id: uuid.UUID | None = None) -> list[TaskRow]:
    with tenant_session(ctx) as db:
        where = [Task.owner_id == ctx.user_id, Task.assignee_id != ctx.user_id]
        if assignee_id:
            where.append(Task.assignee_id == assignee_id)
        if not include_done:
            where.append(Task.status.in_(OPEN))
        return _rows(db, ctx, where)


def recently_done(ctx: TenantContext, days: int = 7) -> list[TaskRow]:
    from datetime import timedelta

    with tenant_session(ctx) as db:
        return _rows(db, ctx, [or_(Task.owner_id == ctx.user_id, Task.assignee_id == ctx.user_id),
                               Task.status == "done", Task.completed_at >= _now() - timedelta(days=days)])


def get(ctx: TenantContext, task_id: uuid.UUID) -> TaskDetail:
    with tenant_session(ctx) as db:
        t = _get(db, ctx, task_id)
        row = _rows(db, ctx, [Task.id == t.id])[0]
        cs = list(db.scalars(select(TaskComment).where(TaskComment.task_id == t.id).order_by(TaskComment.created_at)))
        names = dict(db.execute(select(User.id, User.name).where(User.id.in_({c.author_id for c in cs}))).all()) if cs else {}
        return TaskDetail(**row.__dict__, details=_dec(t.details_enc), completion_note=_dec(t.completion_note_enc),
                          created_at=t.created_at,
                          thread=[CommentView(names.get(c.author_id, ""), c.author_id, _dec(c.body_enc) or "",
                                              c.created_at) for c in cs])


@dataclass
class Summary:
    due_today: int
    overdue: int
    open: int
    delegated_open: int
    delegated_overdue: int
    delegated_done_today: int


def summary(ctx: TenantContext) -> Summary:
    today = ctx.today()
    with tenant_session(ctx) as db:
        mine = db.execute(select(
            func.count().filter(Task.due_on == today), func.count().filter(Task.due_on < today), func.count())
            .where(Task.org_id == ctx.org_id, Task.assignee_id == ctx.user_id, Task.status.in_(OPEN))).one()
        dele = db.execute(select(
            func.count().filter(Task.status.in_(OPEN)),
            func.count().filter(Task.status.in_(OPEN) & (Task.due_on < today)),
            func.count().filter((Task.status == "done") & (func.date(Task.completed_at) == today)))
            .where(Task.org_id == ctx.org_id, Task.owner_id == ctx.user_id, Task.assignee_id != ctx.user_id)).one()
        return Summary(int(mine[0]), int(mine[1]), int(mine[2]), int(dele[0]), int(dele[1]), int(dele[2]))
