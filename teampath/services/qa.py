"""Questions & comments on steps, content and goals, routed up the hierarchy."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from .. import audit, events
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import tenant_session
from ..models import Comment, FaqEntry, Membership, Thread, User
from .notify import after_commit, notify
from .orgs import upline_chain

TARGETS = ("step", "content", "general")


def _now():
    return datetime.now(timezone.utc)


@dataclass
class CommentView:
    id: uuid.UUID
    author_id: uuid.UUID
    author_name: str
    body: str
    is_answer: bool
    created_at: datetime


@dataclass
class ThreadView:
    id: uuid.UUID
    title: str
    status: str
    is_question: bool
    author_id: uuid.UUID
    author_name: str
    target_type: str
    target_id: uuid.UUID | None
    assigned_to_name: str | None
    created_at: datetime
    last_activity_at: datetime
    comments: list[CommentView]
    can_resolve: bool = False
    can_promote: bool = False


def _first_responder(db, ctx: TenantContext) -> uuid.UUID | None:
    m = db.get(Membership, ctx.membership_id)
    chain = upline_chain(db, ctx.org_id, m.path)
    if chain:
        return chain[0].user_id
    return None


def ask(ctx: TenantContext, title: str, body: str, target_type: str = "general",
        target_id: uuid.UUID | None = None, is_question: bool = True) -> uuid.UUID:
    title, body = (title or "").strip(), (body or "").strip()
    if not title:
        raise ValidationError("Add a short title.")
    if not body:
        raise ValidationError("Write your question.")
    if target_type not in TARGETS:
        raise ValidationError("Unknown target.")
    with tenant_session(ctx) as db:
        assignee = _first_responder(db, ctx) if is_question else None
        t = Thread(org_id=ctx.org_id, author_id=ctx.user_id, target_type=target_type, target_id=target_id,
                   title=title[:200], is_question=is_question, assigned_to=assignee)
        db.add(t)
        db.flush()
        db.add(Comment(org_id=ctx.org_id, thread_id=t.id, thread_author_id=ctx.user_id, author_id=ctx.user_id, body=body))
        if assignee:
            notify(db, ctx, assignee, "question", f"{ctx.user_name} asked: {title[:120]}", route=f"/questions/{t.id}")
        audit.record(db, ctx, "qa.asked", "thread", t.id)
        tid = t.id
        after_commit(db, lambda: events.publish(events.org_topic(ctx.org_id), kind="qa", thread_id=str(tid)))
        return t.id


def reply(ctx: TenantContext, thread_id: uuid.UUID, body: str) -> None:
    body = (body or "").strip()
    if not body:
        raise ValidationError("Write a reply.")
    with tenant_session(ctx) as db:
        t = db.get(Thread, thread_id)
        if not t:
            raise NotFound("Thread not found")
        is_answer = t.is_question and ctx.user_id != t.author_id
        db.add(Comment(org_id=ctx.org_id, thread_id=t.id, thread_author_id=t.author_id, author_id=ctx.user_id,
                       body=body, is_answer=is_answer))
        t.last_activity_at = _now()
        if is_answer and t.status == "open":
            t.status = "answered"
        if ctx.user_id != t.author_id:
            notify(db, ctx, t.author_id, "answer", f"{ctx.user_name} replied: {t.title[:120]}", route=f"/questions/{t.id}")
        elif t.assigned_to:
            if t.status == "answered":
                t.status = "open"  # follow-up question from the author re-opens it
            notify(db, ctx, t.assigned_to, "question", f"Follow-up from {ctx.user_name}: {t.title[:100]}",
                   route=f"/questions/{t.id}")
        audit.record(db, ctx, "qa.replied", "thread", t.id)
        after_commit(db, lambda: events.publish(events.org_topic(ctx.org_id), kind="qa", thread_id=str(thread_id)))


def resolve(ctx: TenantContext, thread_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        t = db.get(Thread, thread_id)
        if not t:
            raise NotFound("Thread not found")
        if t.author_id != ctx.user_id and not ctx.is_author:
            raise PermissionDenied("Only the person who asked can resolve this.")
        t.status = "resolved"
        audit.record(db, ctx, "qa.resolved", "thread", t.id)


def promote_to_faq(ctx: TenantContext, thread_id: uuid.UUID, question: str, answer: str) -> uuid.UUID:
    ctx.require_author()
    question, answer = (question or "").strip(), (answer or "").strip()
    if not question or not answer:
        raise ValidationError("Write both the question and the answer.")
    with tenant_session(ctx) as db:
        t = db.get(Thread, thread_id)
        if not t:
            raise NotFound("Thread not found")
        f = FaqEntry(org_id=ctx.org_id, target_type=t.target_type, target_id=t.target_id, question=question,
                     answer=answer, created_by=ctx.user_id)
        db.add(f)
        db.flush()
        audit.record(db, ctx, "qa.promoted_to_faq", "faq", f.id, thread=t.id)
        return f.id


def faq_for(ctx: TenantContext, target_type: str, target_id: uuid.UUID | None) -> list[FaqEntry]:
    with tenant_session(ctx) as db:
        q = select(FaqEntry).where(FaqEntry.org_id == ctx.org_id, FaqEntry.target_type == target_type)
        q = q.where(FaqEntry.target_id == target_id) if target_id else q.where(FaqEntry.target_id.is_(None))
        return list(db.scalars(q.order_by(FaqEntry.created_at)))


def _views(db, ctx, threads: list[Thread], with_comments: bool) -> list[ThreadView]:
    ids = {t.author_id for t in threads} | {t.assigned_to for t in threads if t.assigned_to}
    comments: dict[uuid.UUID, list[Comment]] = {}
    if with_comments and threads:
        for c in db.scalars(select(Comment).where(Comment.thread_id.in_([t.id for t in threads])).order_by(Comment.created_at)):
            comments.setdefault(c.thread_id, []).append(c)
            ids.add(c.author_id)
    names = dict(db.execute(select(User.id, User.name).where(User.id.in_(ids))).all()) if ids else {}
    out = []
    for t in threads:
        out.append(ThreadView(
            t.id, t.title, t.status, t.is_question, t.author_id, names.get(t.author_id, "Former member"),
            t.target_type, t.target_id, names.get(t.assigned_to) if t.assigned_to else None, t.created_at,
            t.last_activity_at,
            [CommentView(c.id, c.author_id, names.get(c.author_id, "Former member"), c.body, c.is_answer, c.created_at)
             for c in comments.get(t.id, [])],
            can_resolve=t.status != "resolved" and (t.author_id == ctx.user_id or ctx.is_author),
            can_promote=ctx.is_author,
        ))
    return out


def threads_for(ctx: TenantContext, target_type: str, target_id: uuid.UUID | None) -> list[ThreadView]:
    """Threads on a step/content that the caller can see (their own, their downline's, or all for admins)."""
    with tenant_session(ctx) as db:
        q = select(Thread).where(Thread.org_id == ctx.org_id, Thread.target_type == target_type)
        q = q.where(Thread.target_id == target_id) if target_id else q
        return _views(db, ctx, list(db.scalars(q.order_by(Thread.created_at.desc()).limit(50))), True)


def get_thread(ctx: TenantContext, thread_id: uuid.UUID) -> ThreadView:
    with tenant_session(ctx) as db:
        t = db.get(Thread, thread_id)
        if not t:
            raise NotFound("Thread not found")
        return _views(db, ctx, [t], True)[0]


def my_questions(ctx: TenantContext) -> list[ThreadView]:
    with tenant_session(ctx) as db:
        ts = list(db.scalars(select(Thread).where(Thread.org_id == ctx.org_id, Thread.author_id == ctx.user_id)
                             .order_by(Thread.last_activity_at.desc()).limit(100)))
        return _views(db, ctx, ts, False)


def inbox(ctx: TenantContext, include_answered: bool = False) -> list[ThreadView]:
    """Questions from the caller's downline (or assigned to them) that need attention."""
    with tenant_session(ctx) as db:
        statuses = ["open", "answered"] if include_answered else ["open"]
        ts = list(db.scalars(
            select(Thread).where(Thread.org_id == ctx.org_id, Thread.is_question.is_(True),
                                 Thread.author_id != ctx.user_id, Thread.status.in_(statuses))
            .order_by(Thread.created_at)
        ))
        return _views(db, ctx, ts, False)
