"""Prospects: a lightweight, follow-up-driven CRM for agents.

Rules:
* Prospects are private to the agent who owns them (row-level security) — leaders
  and admins can't read them. Contact details and notes are encrypted at rest.
* Every open prospect always has a next follow-up date. Logging a touch requires
  choosing the next step (or closing the prospect).
* Logging a call or meeting also counts toward the agent's daily activity.
* No messages are sent from the platform: tap-to-call / tap-to-text open the
  agent's own phone. Do-not-call / no-text flags block those buttons.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update

from .. import audit
from ..config import get_settings
from ..context import NotFound, TenantContext, ValidationError
from ..db import system_session, tenant_session
from ..models import (
    ActivityLog,
    ActivityType,
    Organization,
    Prospect,
    ProspectActivity,
    ProspectList,
    ProspectListItem,
)
from ..security import decrypt, encrypt
from .activity import refresh_momentum

# ------------------------------------------------------------ vocabulary

STAGES = {
    "sales": [("new", "New"), ("contacted", "Contacted"), ("appointment", "Appointment set"),
              ("presented", "Presented"), ("application", "Application"), ("won", "Client – won"),
              ("nurture", "Check back later"), ("lost", "Not interested")],
    "recruit": [("new", "New"), ("contacted", "Contacted"), ("interview", "Interview set"),
                ("interviewed", "Interviewed"), ("licensing", "Licensing"), ("joined", "Joined the team"),
                ("nurture", "Check back later"), ("lost", "Not now")],
}
CLOSED_STAGES = {"won", "joined", "lost"}
KINDS = ("sales", "recruit")
TOUCH_KINDS = [("call", "Call"), ("text", "Text"), ("email", "Email"), ("meeting", "Meeting"), ("note", "Note")]
SOURCES = [("referral", "Referral"), ("event", "Event"), ("social", "Social media"), ("cold", "Cold / list"),
           ("client", "Existing client"), ("other", "Other")]
TEMPERATURES = [("hot", "Hot"), ("warm", "Warm"), ("cold", "Cold")]


@dataclass(frozen=True)
class Outcome:
    key: str
    label: str
    touch_kinds: tuple
    next_days: int | None  # suggested follow-up; None = closes the prospect
    stage: dict  # prospect kind -> stage it moves to (forward only)
    next_action: str = "Follow up"
    kinds: tuple = KINDS  # prospect kinds it applies to


CONVERSATION = ("call", "text", "email", "meeting")
OUTCOMES = [
    Outcome("no_answer", "No answer", ("call",), 1, {}, "Call again"),
    Outcome("voicemail", "Left voicemail", ("call",), 2, {}, "Call again"),
    Outcome("sent", "Sent a message", ("text", "email"), 2, {}, "Check for a reply"),
    Outcome("talked", "Talked – follow up", CONVERSATION, 3, {"sales": "contacted", "recruit": "contacted"}, "Follow up"),
    Outcome("appointment", "Appointment set", CONVERSATION, 2, {"sales": "appointment", "recruit": "interview"},
            "Appointment"),
    Outcome("met", "Met / presented", ("meeting", "call"), 2, {"sales": "presented"}, "Follow up on proposal",
            kinds=("sales",)),
    Outcome("interviewed", "Interview done", ("meeting", "call"), 2, {"recruit": "interviewed"}, "Follow up",
            kinds=("recruit",)),
    Outcome("application", "Application submitted", ("meeting", "call", "note"), 7, {"sales": "application"},
            "Check application status", kinds=("sales",)),
    Outcome("licensing", "Started licensing", ("meeting", "call", "note"), 7, {"recruit": "licensing"},
            "Check licensing progress", kinds=("recruit",)),
    Outcome("won", "Became a client", CONVERSATION + ("note",), None, {"sales": "won"}, kinds=("sales",)),
    Outcome("joined", "Joined the team", CONVERSATION + ("note",), None, {"recruit": "joined"}, kinds=("recruit",)),
    Outcome("later", "Check back later", CONVERSATION + ("note",), 90, {"sales": "nurture", "recruit": "nurture"},
            "Check back in"),
    Outcome("not_interested", "Not interested", CONVERSATION + ("note",), None, {"sales": "lost", "recruit": "lost"}),
    Outcome("note", "Just a note", ("note",), 0, {}),
]
OUTCOME = {o.key: o for o in OUTCOMES}
CONVERSATION_OUTCOMES = {"talked", "appointment", "met", "interviewed", "application", "licensing", "won", "joined",
                         "later", "not_interested"}


def stage_label(kind: str, stage: str) -> str:
    return dict(STAGES.get(kind, STAGES["sales"])).get(stage, stage.title())


def outcomes_for(touch_kind: str, prospect_kind: str) -> list[Outcome]:
    return [o for o in OUTCOMES if touch_kind in o.touch_kinds and prospect_kind in o.kinds]


def _stage_rank(kind: str, stage: str) -> int:
    order = [s for s, _ in STAGES[kind] if s not in ("nurture", "lost")]
    return order.index(stage) if stage in order else -1


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------ contact details

def normalize_phone(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if not 7 <= len(digits) <= 15:
        raise ValidationError("That phone number doesn't look right.")
    return digits


def normalize_email(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    e = raw.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e) or len(e) > 254:
        raise ValidationError("That email address doesn't look right.")
    return e


def _hash(kind: str, value: str | None) -> str | None:
    """Keyed one-way hash used to find duplicates without decrypting anything."""
    if not value:
        return None
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def _enc(value: str | None) -> str | None:
    return encrypt(value) if value else None


def _dec(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return decrypt(value)
    except Exception:
        return None


def format_phone(digits: str | None) -> str:
    if not digits:
        return ""
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return digits


class DuplicateProspect(ValidationError):
    def __init__(self, existing_id: uuid.UUID, name: str):
        super().__init__(f"You already have {name} with that phone or email.")
        self.existing_id = existing_id


# ------------------------------------------------------------ create / edit

def _find_duplicate(db, ctx, phone_hash, email_hash, exclude=None):
    conds = [h for h in (Prospect.phone_hash == phone_hash if phone_hash else None,
                         Prospect.email_hash == email_hash if email_hash else None) if h is not None]
    if not conds:
        return None
    q = select(Prospect).where(Prospect.owner_id == ctx.user_id, Prospect.org_id == ctx.org_id, or_(*conds))
    if exclude:
        q = q.where(Prospect.id != exclude)
    return db.scalars(q.limit(1)).first()


def create(ctx: TenantContext, *, name: str, kind: str = "sales", phone: str = "", email: str = "",
           source: str | None = None, referred_by: str = "", temperature: str = "warm", tags: list[str] | None = None,
           details: str = "", next_on: date | None = None, next_action: str = "", recruit_interest: bool = False,
           preferred_contact: str | None = None, best_time: str = "", dnc: bool = False, no_text: bool = False,
           consent_note: str = "", allow_duplicate: bool = False) -> uuid.UUID:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Enter the prospect's name.")
    if kind not in KINDS:
        raise ValidationError("Choose sales or recruiting.")
    if temperature not in dict(TEMPERATURES):
        raise ValidationError("Choose hot, warm or cold.")
    if source and source not in dict(SOURCES):
        raise ValidationError("Unknown source.")
    phone_n, email_n = normalize_phone(phone), normalize_email(email)
    with tenant_session(ctx) as db:
        if not allow_duplicate:
            dup = _find_duplicate(db, ctx, _hash("phone", phone_n), _hash("email", email_n))
            if dup:
                raise DuplicateProspect(dup.id, dup.name)
        p = Prospect(org_id=ctx.org_id, owner_id=ctx.user_id, kind=kind, recruit_interest=bool(recruit_interest),
                     name=name[:160], phone_enc=_enc(phone_n), email_enc=_enc(email_n),
                     phone_hash=_hash("phone", phone_n), email_hash=_hash("email", email_n),
                     details_enc=_enc((details or "").strip() or None), preferred_contact=preferred_contact or None,
                     best_time=(best_time or "").strip()[:60] or None, source=source or None,
                     referred_by=(referred_by or "").strip()[:120] or None, temperature=temperature,
                     tags=_tags(tags), next_action=(next_action or "First contact").strip()[:160],
                     next_action_on=next_on or ctx.today(), dnc=bool(dnc), no_text=bool(no_text),
                     consent_note=(consent_note or "").strip()[:200] or None)
        db.add(p)
        db.flush()
        audit.record(db, ctx, "prospect.created", "prospect", p.id, kind=kind, source=source)
        return p.id


def _tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = tags.split(",")
    return sorted({t.strip().lower()[:40] for t in (tags or []) if t and t.strip()})[:15]


def _get(db, ctx, prospect_id) -> Prospect:
    p = db.get(Prospect, prospect_id)  # RLS: only the owner's prospects are visible at all
    if not p or p.owner_id != ctx.user_id:
        raise NotFound("Prospect not found")
    return p


def update_details(ctx: TenantContext, prospect_id: uuid.UUID, **f) -> None:
    with tenant_session(ctx) as db:
        p = _get(db, ctx, prospect_id)
        if "name" in f:
            if not (f["name"] or "").strip():
                raise ValidationError("Enter the prospect's name.")
            p.name = f["name"].strip()[:160]
        if "phone" in f:
            n = normalize_phone(f["phone"])
            if n and _find_duplicate(db, ctx, _hash("phone", n), None, exclude=p.id):
                raise ValidationError("Another prospect already has that phone number.")
            p.phone_enc, p.phone_hash = _enc(n), _hash("phone", n)
        if "email" in f:
            n = normalize_email(f["email"])
            if n and _find_duplicate(db, ctx, None, _hash("email", n), exclude=p.id):
                raise ValidationError("Another prospect already has that email.")
            p.email_enc, p.email_hash = _enc(n), _hash("email", n)
        if "details" in f:
            p.details_enc = _enc((f["details"] or "").strip() or None)
        if "kind" in f:
            if f["kind"] not in KINDS:
                raise ValidationError("Choose sales or recruiting.")
            if f["kind"] != p.kind:
                p.kind = f["kind"]
                if p.stage not in dict(STAGES[p.kind]):
                    p.stage = "contacted" if p.last_touch_at else "new"
        if "temperature" in f:
            if f["temperature"] not in dict(TEMPERATURES):
                raise ValidationError("Choose hot, warm or cold.")
            p.temperature = f["temperature"]
        if "source" in f:
            if f["source"] and f["source"] not in dict(SOURCES):
                raise ValidationError("Unknown source.")
            p.source = f["source"] or None
        if "tags" in f:
            p.tags = _tags(f["tags"])
        for k, n in (("referred_by", 120), ("best_time", 60), ("consent_note", 200)):
            if k in f:
                setattr(p, k, (f[k] or "").strip()[:n] or None)
        for k in ("dnc", "no_text", "recruit_interest"):
            if k in f:
                setattr(p, k, bool(f[k]))
        if "preferred_contact" in f:
            p.preferred_contact = f["preferred_contact"] or None


def set_stage(ctx: TenantContext, prospect_id: uuid.UUID, stage: str) -> None:
    with tenant_session(ctx) as db:
        p = _get(db, ctx, prospect_id)
        if stage not in dict(STAGES[p.kind]):
            raise ValidationError("Unknown stage.")
        old = p.stage
        p.stage = stage
        if stage in CLOSED_STAGES:
            p.closed_at, p.next_action_on, p.next_action, p.next_action_time = _now(), None, None, None
        elif p.closed_at or not p.next_action_on:
            p.closed_at = None
            p.next_action_on = p.next_action_on or ctx.today()
            p.next_action = p.next_action or "Follow up"
        db.add(ProspectActivity(org_id=ctx.org_id, owner_id=ctx.user_id, prospect_id=p.id, kind="note",
                                outcome="stage", stage_from=old, stage_to=stage))


def set_next(ctx: TenantContext, prospect_id: uuid.UUID, on: date, action: str = "", at: str | None = None) -> None:
    if not on:
        raise ValidationError("Pick a date for the next follow-up.")
    if on < ctx.today():
        raise ValidationError("The next follow-up can't be in the past.")
    with tenant_session(ctx) as db:
        p = _get(db, ctx, prospect_id)
        if p.stage in CLOSED_STAGES:
            raise ValidationError("Reopen the prospect first (change its stage).")
        p.next_action_on, p.next_action_time = on, _clean_time(at)
        p.next_action = (action or p.next_action or "Follow up").strip()[:160]


def _clean_time(at: str | None) -> str | None:
    if not at:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", at.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValidationError("Use a time like 14:30.")
    return f"{int(m[1]):02d}:{m[2]}"


def delete_prospect(ctx: TenantContext, prospect_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        p = _get(db, ctx, prospect_id)
        db.delete(p)
        audit.record(db, ctx, "prospect.deleted", "prospect", prospect_id)


def set_archived(ctx: TenantContext, prospect_id: uuid.UUID, archived: bool) -> None:
    with tenant_session(ctx) as db:
        p = _get(db, ctx, prospect_id)
        p.archived_at = _now() if archived else None
        if not archived:
            p.last_touch_at = p.last_touch_at or _now()  # restart the retention clock


# ------------------------------------------------------------ logging touches

@dataclass
class LogResult:
    stage: str
    counted: list[str]
    closed: bool


# What an assistant may record on a delegated prospect. Closing a prospect, presenting or
# taking an application stays with the (licensed) agent.
ASSISTANT_OUTCOMES = {"no_answer", "voicemail", "sent", "talked", "appointment", "later", "note"}


def log_touch(ctx: TenantContext, prospect_id: uuid.UUID, *, kind: str, outcome: str, note: str = "",
              next_on: date | None = None, next_action: str = "", next_time: str | None = None,
              list_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None) -> LogResult:
    """Record a call/text/email/meeting/note and set what happens next.

    With `task_id`, an assistant logs on the agent's behalf through an open delegated
    task (the database only lets them see that prospect while the task is open)."""
    o = OUTCOME.get(outcome)
    if kind not in dict(TOUCH_KINDS):
        raise ValidationError("Choose call, text, email, meeting or note.")
    if not o or kind not in o.touch_kinds:
        raise ValidationError("Choose an outcome.")
    if task_id and o.key not in ASSISTANT_OUTCOMES:
        raise ValidationError("Only the agent can record that outcome.")
    today = ctx.today()
    with tenant_session(ctx) as db:
        if task_id:
            from ..models import Task

            t = db.get(Task, task_id)
            if not t or t.assignee_id != ctx.user_id or t.prospect_id != prospect_id \
                    or t.status not in ("open", "in_progress"):
                raise NotFound("Prospect not found")
            p = db.get(Prospect, prospect_id)
            if not p:
                raise NotFound("Prospect not found")
        else:
            p = _get(db, ctx, prospect_id)
        if p.kind not in o.kinds:
            raise ValidationError("That outcome doesn't apply to this prospect.")
        if kind == "call" and p.dnc:
            raise ValidationError(f"{p.name} is marked do-not-call.")
        if kind == "text" and p.no_text:
            raise ValidationError(f"{p.name} asked not to be texted.")
        closing = o.next_days is None
        if not closing and o.key != "note":
            if not next_on:
                raise ValidationError("Pick the next follow-up date.")
            if next_on < today:
                raise ValidationError("The next follow-up can't be in the past.")
        old = p.stage
        target = o.stage.get(p.kind)
        if target:
            if target in CLOSED_STAGES or target == "nurture" or old in ("nurture", "lost") \
                    or _stage_rank(p.kind, target) > _stage_rank(p.kind, old):
                p.stage = target
        elif old == "new" and kind in CONVERSATION and o.key != "note":
            pass  # an attempt without a conversation keeps the prospect "new"
        if closing:
            p.closed_at, p.next_action_on, p.next_action, p.next_action_time = _now(), None, None, None
        elif o.key != "note" or next_on:
            p.closed_at = None
            p.next_action_on = next_on
            p.next_action_time = _clean_time(next_time)
            p.next_action = (next_action or o.next_action).strip()[:160]
        if o.key != "note":
            p.last_touch_at = _now()
        db.add(ProspectActivity(org_id=ctx.org_id, owner_id=p.owner_id, prospect_id=p.id, kind=kind,
                                outcome=o.key, note_enc=_enc((note or "").strip() or None),
                                stage_from=old if p.stage != old else None, stage_to=p.stage if p.stage != old else None,
                                by_user_id=ctx.user_id if task_id else None))
        # An assistant's calls aren't the agent's own activity, so they aren't counted.
        counted = [] if task_id else _count_activity(db, ctx, kind, o, p.kind, today)
        if list_id:
            db.execute(update(ProspectListItem).where(ProspectListItem.list_id == list_id,
                                                      ProspectListItem.prospect_id == p.id)
                       .values(done_at=_now(), outcome=o.key))
            _maybe_complete_list(db, list_id)
        result = LogResult(p.stage, counted, closing)
    if counted:
        from .. import events

        events.publish(events.org_topic(ctx.org_id), kind="activity", user_id=str(ctx.user_id))
    return result


def _count_activity(db, ctx, kind: str, o: Outcome, prospect_kind: str, day: date) -> list[str]:
    """Also count the touch toward the agent's daily activity (no double entry)."""
    keys = []
    if kind == "call":
        keys.append("dials")
    if o.key in CONVERSATION_OUTCOMES and kind in ("call", "meeting", "text", "email"):
        keys.append("recruiting" if prospect_kind == "recruit" else "contacts")
    if o.key == "appointment" and prospect_kind == "sales":
        keys.append("appts_set")
    if o.key == "met":
        keys.append("appts_held")
    if o.key == "application":
        keys.append("apps")
    if not keys:
        return []
    types = {t.key: t for t in db.scalars(select(ActivityType).where(
        ActivityType.org_id == ctx.org_id, ActivityType.key.in_(keys), ActivityType.active.is_(True)))}
    labels = []
    for k in dict.fromkeys(keys):
        t = types.get(k)
        if t:
            db.add(ActivityLog(org_id=ctx.org_id, user_id=ctx.user_id, type_id=t.id, count=1, logged_on=day,
                               note="Prospect follow-up"))
            labels.append(t.label)
    if labels:
        db.flush()
        refresh_momentum(db, ctx.org_id, ctx.user_id, day)
    return labels


# ------------------------------------------------------------ reading

@dataclass
class Row:
    id: uuid.UUID
    name: str
    kind: str
    stage: str
    temperature: str
    tags: list[str]
    next_action: str | None
    next_on: date | None
    next_time: str | None
    last_touch_at: datetime | None
    source: str | None
    recruit_interest: bool
    dnc: bool
    no_text: bool
    has_phone: bool
    has_email: bool
    archived: bool

    @property
    def stage_label(self) -> str:
        return stage_label(self.kind, self.stage)

    @property
    def closed(self) -> bool:
        return self.stage in CLOSED_STAGES


@dataclass
class Touch:
    id: uuid.UUID
    kind: str
    outcome: str | None
    note: str | None
    stage_from: str | None
    stage_to: str | None
    created_at: datetime
    by_name: str | None = None  # set when an assistant logged it

    @property
    def outcome_label(self) -> str:
        if self.outcome == "stage":
            return "Stage changed"
        return OUTCOME[self.outcome].label if self.outcome in OUTCOME else (self.outcome or "")


@dataclass
class Detail(Row):
    phone: str | None = None
    email: str | None = None
    details: str | None = None
    referred_by: str | None = None
    preferred_contact: str | None = None
    best_time: str | None = None
    consent_note: str | None = None
    created_at: datetime | None = None
    touches: list[Touch] = field(default_factory=list)


def _row(p: Prospect) -> Row:
    return Row(p.id, p.name, p.kind, p.stage, p.temperature, list(p.tags or []), p.next_action, p.next_action_on,
               p.next_action_time, p.last_touch_at, p.source, p.recruit_interest, p.dnc, p.no_text,
               bool(p.phone_enc), bool(p.email_enc), p.archived_at is not None)


def get(ctx: TenantContext, prospect_id: uuid.UUID) -> Detail:
    with tenant_session(ctx) as db:
        p = _get(db, ctx, prospect_id)
        r = _row(p)
        touches = _touches(db, p.id)
        return Detail(**r.__dict__, phone=_dec(p.phone_enc), email=_dec(p.email_enc), details=_dec(p.details_enc),
                      referred_by=p.referred_by, preferred_contact=p.preferred_contact, best_time=p.best_time,
                      consent_note=p.consent_note, created_at=p.created_at, touches=touches)


def _touches(db, prospect_id, limit: int = 200) -> list[Touch]:
    from ..models import User

    acts = list(db.scalars(select(ProspectActivity).where(ProspectActivity.prospect_id == prospect_id)
                           .order_by(ProspectActivity.created_at.desc()).limit(limit)))
    by = {a.by_user_id for a in acts if a.by_user_id}
    names = dict(db.execute(select(User.id, User.name).where(User.id.in_(by))).all()) if by else {}
    return [Touch(a.id, a.kind, a.outcome, _dec(a.note_enc), a.stage_from, a.stage_to, a.created_at,
                  names.get(a.by_user_id) if a.by_user_id else None) for a in acts]


def get_for_task(ctx: TenantContext, task_id: uuid.UUID) -> Detail | None:
    """What an assistant sees of a delegated prospect: contact details, stage, next step and
    recent activity — not the agent's private notes."""
    from ..models import Task

    with tenant_session(ctx) as db:
        t = db.get(Task, task_id)
        if not t or not t.prospect_id:
            return None
        p = db.get(Prospect, t.prospect_id)  # visible to the assignee only while the task is open
        if not p:
            return None
        r = _row(p)
        own = p.owner_id == ctx.user_id
        return Detail(**r.__dict__, phone=_dec(p.phone_enc), email=_dec(p.email_enc),
                      details=_dec(p.details_enc) if own else None, referred_by=p.referred_by,
                      preferred_contact=p.preferred_contact, best_time=p.best_time, consent_note=p.consent_note,
                      created_at=p.created_at, touches=_touches(db, p.id, 10))


def _apply_filter(q, ctx: TenantContext, f: dict):
    today = ctx.today()
    q = q.where(Prospect.owner_id == ctx.user_id, Prospect.org_id == ctx.org_id)
    q = q.where(Prospect.archived_at.is_not(None) if f.get("archived") else Prospect.archived_at.is_(None))
    if not f.get("include_closed") and not f.get("archived"):
        q = q.where(Prospect.stage.not_in(CLOSED_STAGES))
    if f.get("kind") in KINDS:
        q = q.where(Prospect.kind == f["kind"])
    if f.get("stages"):
        q = q.where(Prospect.stage.in_(f["stages"]))
    if f.get("temperature"):
        q = q.where(Prospect.temperature.in_(f["temperature"]))
    if f.get("tags"):
        q = q.where(Prospect.tags.overlap([t.lower() for t in f["tags"]]))
    if f.get("source"):
        q = q.where(Prospect.source == f["source"])
    if f.get("recruit_interest"):
        q = q.where(or_(Prospect.recruit_interest.is_(True), Prospect.kind == "recruit"))
    due = f.get("due")
    if due == "today":
        q = q.where(Prospect.next_action_on == today)
    elif due == "overdue":
        q = q.where(Prospect.next_action_on < today)
    elif due == "due":
        q = q.where(Prospect.next_action_on <= today)
    elif due == "week":
        q = q.where(Prospect.next_action_on <= today + timedelta(days=7))
    if f.get("untouched_days"):
        cutoff = _now() - timedelta(days=int(f["untouched_days"]))
        q = q.where(func.coalesce(Prospect.last_touch_at, Prospect.created_at) < cutoff)
    if f.get("created_within_days"):
        q = q.where(Prospect.created_at >= _now() - timedelta(days=int(f["created_within_days"])))
    text_ = (f.get("text") or "").strip()
    if text_:
        conds = [Prospect.name.ilike(f"%{text_}%"), func.array_to_string(Prospect.tags, " ").ilike(f"%{text_.lower()}%")]
        try:
            if "@" in text_:
                conds.append(Prospect.email_hash == _hash("email", normalize_email(text_)))
            elif len(re.sub(r"\D", "", text_)) >= 7:
                conds.append(Prospect.phone_hash == _hash("phone", normalize_phone(text_)))
        except ValidationError:
            pass
        q = q.where(or_(*conds))
    return q


def _ordered(q):
    return q.order_by(Prospect.next_action_on.asc().nulls_last(), Prospect.next_action_time.asc().nulls_last(),
                      Prospect.name)


def search(ctx: TenantContext, f: dict | None = None, limit: int = 500) -> list[Row]:
    with tenant_session(ctx) as db:
        q = _ordered(_apply_filter(select(Prospect), ctx, f or {})).limit(limit)
        return [_row(p) for p in db.scalars(q)]


def count(ctx: TenantContext, f: dict) -> int:
    with tenant_session(ctx) as db:
        return db.scalar(_apply_filter(select(func.count()).select_from(Prospect), ctx, f)) or 0


def due_counts(ctx: TenantContext) -> tuple[int, int]:
    return count(ctx, {"due": "today"}), count(ctx, {"due": "overdue"})


def pipeline_counts(ctx: TenantContext, kind: str) -> list[tuple[str, str, int]]:
    with tenant_session(ctx) as db:
        rows = dict(db.execute(select(Prospect.stage, func.count()).where(
            Prospect.owner_id == ctx.user_id, Prospect.org_id == ctx.org_id, Prospect.kind == kind,
            Prospect.archived_at.is_(None)).group_by(Prospect.stage)).all())
    return [(s, label, int(rows.get(s, 0))) for s, label in STAGES[kind]]


# ------------------------------------------------------------ lists

BUILTIN_LISTS = [
    ("due", "All follow-ups due now", {"due": "due"}, "Overdue and today, oldest first"),
    ("overdue", "Overdue", {"due": "overdue"}, "Follow-ups you've missed"),
    ("due_today", "Due today", {"due": "today"}, "Today's follow-ups"),
    ("hot", "Hot – no appointment yet", {"temperature": ["hot"], "stages": ["new", "contacted"]},
     "Your warmest leads to book"),
    ("stale", "No contact in 14+ days", {"untouched_days": 14}, "Before they go cold"),
    ("new_referrals", "New referrals (7 days)", {"source": "referral", "created_within_days": 7},
     "Call referrals while they're fresh"),
    ("recruit_licensing", "Recruits in licensing", {"kind": "recruit", "stages": ["licensing"]},
     "Keep them moving"),
    ("recruit_interest", "Could join the team", {"recruit_interest": True}, "Recruiting conversations"),
    ("nurture", "Check back later", {"stages": ["nurture"]}, "Long-term follow-ups"),
]
BUILTIN = {k: (name, f) for k, name, f, _ in BUILTIN_LISTS}


@dataclass
class ListInfo:
    id: str  # builtin key or uuid
    name: str
    kind: str  # builtin | smart | focus
    filter: dict
    count: int
    done: int = 0
    total: int = 0
    completed: bool = False
    description: str = ""


def lists(ctx: TenantContext) -> list[ListInfo]:
    out = [ListInfo(k, name, "builtin", f, count(ctx, f), description=desc) for k, name, f, desc in BUILTIN_LISTS]
    with tenant_session(ctx) as db:
        mine = list(db.scalars(select(ProspectList).where(ProspectList.owner_id == ctx.user_id,
                                                          ProspectList.org_id == ctx.org_id)
                               .order_by(ProspectList.completed_at.desc().nulls_first(), ProspectList.created_at.desc())))
        progress = dict((lid, (done, total)) for lid, done, total in db.execute(
            select(ProspectListItem.list_id, func.count(ProspectListItem.done_at), func.count())
            .where(ProspectListItem.owner_id == ctx.user_id).group_by(ProspectListItem.list_id)).all())
    for lst in mine:
        if lst.kind == "smart":
            out.append(ListInfo(str(lst.id), lst.name, "smart", lst.filter or {}, count(ctx, lst.filter or {})))
        else:
            done, total = progress.get(lst.id, (0, 0))
            out.append(ListInfo(str(lst.id), lst.name, "focus", {}, total - done, done, total, lst.completed_at is not None))
    return out


def save_smart_list(ctx: TenantContext, name: str, f: dict) -> uuid.UUID:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Name the list.")
    allowed = {"kind", "stages", "temperature", "tags", "source", "recruit_interest", "due", "untouched_days",
               "created_within_days", "text", "include_closed"}
    f = {k: v for k, v in (f or {}).items() if k in allowed and v not in (None, "", [], False)}
    with tenant_session(ctx) as db:
        lst = ProspectList(org_id=ctx.org_id, owner_id=ctx.user_id, name=name[:120], kind="smart", filter=f)
        db.add(lst)
        db.flush()
        return lst.id


def delete_list(ctx: TenantContext, list_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        lst = db.get(ProspectList, list_id)
        if not lst or lst.owner_id != ctx.user_id:
            raise NotFound("List not found")
        db.delete(lst)


def resolve_filter(ctx: TenantContext, list_id: str) -> tuple[str, dict]:
    if list_id in BUILTIN:
        return BUILTIN[list_id]
    with tenant_session(ctx) as db:
        lst = db.get(ProspectList, uuid.UUID(list_id))
        if not lst or lst.owner_id != ctx.user_id or lst.kind != "smart":
            raise NotFound("List not found")
        return lst.name, dict(lst.filter or {})


def create_focus_list(ctx: TenantContext, name: str, prospect_ids: list[uuid.UUID]) -> uuid.UUID:
    """A list to work through now, one person at a time."""
    name = (name or "").strip() or f"Focus – {ctx.today():%b %d}"
    if not prospect_ids:
        raise ValidationError("There's no one to add to this list.")
    if len(prospect_ids) > 300:
        raise ValidationError("Keep focus lists to 300 people or fewer.")
    with tenant_session(ctx) as db:
        owned = set(db.scalars(select(Prospect.id).where(Prospect.id.in_(prospect_ids),
                                                         Prospect.owner_id == ctx.user_id)))
        lst = ProspectList(org_id=ctx.org_id, owner_id=ctx.user_id, name=name[:120], kind="focus")
        db.add(lst)
        db.flush()
        pos = 0
        for pid in dict.fromkeys(prospect_ids):
            if pid in owned:
                db.add(ProspectListItem(org_id=ctx.org_id, owner_id=ctx.user_id, list_id=lst.id, prospect_id=pid,
                                        position=pos))
                pos += 1
        if not pos:
            raise ValidationError("There's no one to add to this list.")
        return lst.id


def focus_from(ctx: TenantContext, list_id: str, name: str | None = None) -> uuid.UUID:
    """Snapshot a built-in or saved smart list into a focus list."""
    title, f = resolve_filter(ctx, list_id)
    rows = search(ctx, f, limit=300)
    return create_focus_list(ctx, name or f"{title} – {ctx.today():%b %d}", [r.id for r in rows])


def add_to_focus(ctx: TenantContext, list_id: uuid.UUID, prospect_ids: list[uuid.UUID]) -> int:
    with tenant_session(ctx) as db:
        lst = db.get(ProspectList, list_id)
        if not lst or lst.owner_id != ctx.user_id or lst.kind != "focus":
            raise NotFound("List not found")
        existing = set(db.scalars(select(ProspectListItem.prospect_id).where(ProspectListItem.list_id == list_id)))
        owned = set(db.scalars(select(Prospect.id).where(Prospect.id.in_(prospect_ids), Prospect.owner_id == ctx.user_id)))
        pos = db.scalar(select(func.coalesce(func.max(ProspectListItem.position), -1)).where(
            ProspectListItem.list_id == list_id)) + 1
        n = 0
        for pid in prospect_ids:
            if pid in owned and pid not in existing:
                db.add(ProspectListItem(org_id=ctx.org_id, owner_id=ctx.user_id, list_id=list_id, prospect_id=pid,
                                        position=pos + n))
                n += 1
        if n:
            lst.completed_at = None
        return n


def _maybe_complete_list(db, list_id) -> None:
    left = db.scalar(select(func.count()).select_from(ProspectListItem).where(
        ProspectListItem.list_id == list_id, ProspectListItem.done_at.is_(None)))
    lst = db.get(ProspectList, list_id)
    if lst and lst.kind == "focus":
        lst.completed_at = _now() if left == 0 else None


@dataclass
class FocusState:
    list_id: uuid.UUID
    name: str
    total: int
    done: int
    current: Detail | None
    outcomes: dict  # outcome key -> count
    remaining_names: list[str]


def focus_state(ctx: TenantContext, list_id: uuid.UUID) -> FocusState:
    with tenant_session(ctx) as db:
        lst = db.get(ProspectList, list_id)
        if not lst or lst.owner_id != ctx.user_id or lst.kind != "focus":
            raise NotFound("List not found")
        items = list(db.execute(select(ProspectListItem, Prospect.name)
                                .join(Prospect, Prospect.id == ProspectListItem.prospect_id)
                                .where(ProspectListItem.list_id == list_id).order_by(ProspectListItem.position)).all())
        name = lst.name
    pending = [(i, n) for i, n in items if i.done_at is None]
    outcomes: dict = {}
    for i, _ in items:
        if i.outcome:
            outcomes[i.outcome] = outcomes.get(i.outcome, 0) + 1
    current = get(ctx, pending[0][0].prospect_id) if pending else None
    return FocusState(list_id, name, len(items), len(items) - len(pending), current, outcomes,
                      [n for _, n in pending[1:6]])


def skip(ctx: TenantContext, list_id: uuid.UUID, prospect_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        db.execute(update(ProspectListItem).where(ProspectListItem.list_id == list_id,
                                                  ProspectListItem.prospect_id == prospect_id,
                                                  ProspectListItem.owner_id == ctx.user_id)
                   .values(done_at=_now(), outcome="skipped"))
        _maybe_complete_list(db, list_id)


# ------------------------------------------------------------ import / export

HEADER_ALIASES = {
    "name": ("name", "full name", "fullname", "contact", "contact name", "prospect"),
    "first": ("first", "first name", "firstname", "given name"),
    "last": ("last", "last name", "lastname", "surname", "family name"),
    "phone": ("phone", "phone number", "mobile", "cell", "cell phone", "mobile phone", "telephone", "tel"),
    "email": ("email", "e-mail", "email address", "mail"),
    "source": ("source", "lead source"),
    "referred_by": ("referred by", "referrer", "referral"),
    "tags": ("tags", "tag", "labels", "groups"),
    "details": ("notes", "note", "details", "comments"),
    "kind": ("type", "kind", "prospect type"),
    "temperature": ("temperature", "rating", "priority"),
}
MAX_IMPORT_ROWS = 5000


@dataclass
class ImportPreview:
    columns: dict  # field -> header
    rows: int
    unmapped: list[str]


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValidationError("CSV files can be up to 5 MB.")
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text_ = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text_[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text_), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValidationError("The file needs a header row and at least one prospect.")
    if len(rows) - 1 > MAX_IMPORT_ROWS:
        raise ValidationError(f"Import up to {MAX_IMPORT_ROWS} prospects at a time.")
    return [h.strip() for h in rows[0]], rows[1:]


def _map_headers(headers: list[str]) -> dict:
    mapping = {}
    for idx, h in enumerate(headers):
        key = h.strip().lower()
        for fld, aliases in HEADER_ALIASES.items():
            if key in aliases and fld not in mapping:
                mapping[fld] = idx
    if "name" not in mapping and "first" not in mapping:
        raise ValidationError("Couldn't find a Name (or First name) column.")
    return mapping


def preview_csv(path: Path) -> ImportPreview:
    headers, rows = _read_csv(path)
    mapping = _map_headers(headers)
    used = set(mapping.values())
    return ImportPreview({f: headers[i] for f, i in mapping.items()}, len(rows),
                         [h for i, h in enumerate(headers) if i not in used and h])


@dataclass
class ImportResult:
    created: int = 0
    duplicates: int = 0
    skipped: int = 0
    problems: list[str] = field(default_factory=list)


def import_csv(ctx: TenantContext, path: Path, default_kind: str = "sales", tag: str = "") -> ImportResult:
    headers, rows = _read_csv(path)
    m = _map_headers(headers)
    res = ImportResult()
    extra_tags = _tags([tag]) if tag else []

    def cell(r, f):
        i = m.get(f)
        return r[i].strip() if i is not None and i < len(r) else ""

    with tenant_session(ctx) as db:
        seen_phone = set(db.scalars(select(Prospect.phone_hash).where(Prospect.owner_id == ctx.user_id,
                                                                     Prospect.phone_hash.is_not(None))))
        seen_email = set(db.scalars(select(Prospect.email_hash).where(Prospect.owner_id == ctx.user_id,
                                                                     Prospect.email_hash.is_not(None))))
        for n, r in enumerate(rows, start=2):
            name = cell(r, "name") or " ".join(x for x in (cell(r, "first"), cell(r, "last")) if x)
            if not name:
                res.skipped += 1
                continue
            try:
                phone, email = normalize_phone(cell(r, "phone")), normalize_email(cell(r, "email"))
            except ValidationError as e:
                res.skipped += 1
                if len(res.problems) < 10:
                    res.problems.append(f"Row {n} ({name}): {e}")
                continue
            ph, eh = _hash("phone", phone), _hash("email", email)
            if (ph and ph in seen_phone) or (eh and eh in seen_email):
                res.duplicates += 1
                continue
            kind = cell(r, "kind").lower()
            kind = "recruit" if kind.startswith("recruit") else ("sales" if kind else default_kind)
            src = cell(r, "source").lower()
            src = src if src in dict(SOURCES) else ("referral" if cell(r, "referred_by") else (None if not src else "other"))
            temp = cell(r, "temperature").lower()
            temp = temp if temp in dict(TEMPERATURES) else "warm"
            db.add(Prospect(org_id=ctx.org_id, owner_id=ctx.user_id, kind=kind, name=name[:160],
                            phone_enc=_enc(phone), email_enc=_enc(email), phone_hash=ph, email_hash=eh,
                            details_enc=_enc(cell(r, "details") or None), source=src,
                            referred_by=cell(r, "referred_by")[:120] or None, temperature=temp,
                            tags=_tags(cell(r, "tags").replace(";", ",").split(",") + extra_tags),
                            next_action="First contact", next_action_on=ctx.today()))
            seen_phone.add(ph) if ph else None
            seen_email.add(eh) if eh else None
            res.created += 1
        audit.record(db, ctx, "prospects.imported", "prospect", None, created=res.created,
                     duplicates=res.duplicates, skipped=res.skipped)
    path.unlink(missing_ok=True)
    return res


def _export_dir() -> Path:
    d = get_settings().data_dir / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_csv(ctx: TenantContext) -> str:
    """Writes the agent's prospects to a short-lived file; returns a signed download URL."""
    from ..security import _sign

    with tenant_session(ctx) as db:
        ps = list(db.scalars(select(Prospect).where(Prospect.owner_id == ctx.user_id, Prospect.org_id == ctx.org_id)
                             .order_by(Prospect.name)))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Name", "Type", "Stage", "Temperature", "Phone", "Email", "Source", "Referred by", "Tags",
                    "Next action", "Next action date", "Do not call", "No texts", "Notes", "Created", "Archived"])
        for p in ps:
            w.writerow([p.name, p.kind, stage_label(p.kind, p.stage), p.temperature, format_phone(_dec(p.phone_enc)),
                        _dec(p.email_enc) or "", p.source or "", p.referred_by or "", ", ".join(p.tags or []),
                        p.next_action or "", p.next_action_on or "", "yes" if p.dnc else "", "yes" if p.no_text else "",
                        _dec(p.details_enc) or "", p.created_at.date(), "yes" if p.archived_at else ""])
        audit.record(db, ctx, "prospects.exported", "prospect", None, count=len(ps))
    token = secrets.token_urlsafe(24)
    path = _export_dir() / f"{token}.csv"
    path.write_text(buf.getvalue(), encoding="utf-8-sig")
    path.chmod(0o600)
    exp = int(time.time()) + 300
    sig = _sign(f"export|{token}|{ctx.user_id}|{exp}")
    return f"/exports/{token}?u={ctx.user_id}&e={exp}&s={sig}"


def verify_export(token: str, user_id: str, exp: int, sig: str) -> Path | None:
    from ..security import _sign

    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token or "") or exp < time.time():
        return None
    if not hmac.compare_digest(_sign(f"export|{token}|{user_id}|{exp}"), sig or ""):
        return None
    p = _export_dir() / f"{token}.csv"
    return p if p.exists() else None


# ------------------------------------------------------------ ownership & retention (system jobs)

RELEASE_GRACE_DAYS = 30


def mark_owner_released(org_id, user_id) -> None:
    """Agent-owned prospects: when an agent leaves, nobody else gets them; they're deleted after a grace period."""
    with system_session("prospects: owner released from organization") as db:
        db.execute(update(Prospect).where(Prospect.org_id == org_id, Prospect.owner_id == user_id)
                   .values(owner_left_at=_now()))


def apply_retention() -> dict:
    """Daily job: archive inactive prospects and delete old ones, per organization settings."""
    from .orgs import DEFAULT_SETTINGS

    stats = {"archived": 0, "deleted": 0, "released_deleted": 0}
    with system_session("worker: prospect retention") as db:
        stats["released_deleted"] = db.execute(delete(Prospect).where(
            Prospect.owner_left_at < _now() - timedelta(days=RELEASE_GRACE_DAYS))).rowcount
        for org in db.scalars(select(Organization)):
            s = {**DEFAULT_SETTINGS, **(org.settings or {})}
            archive_m, delete_m = int(s["prospect_archive_months"]), int(s["prospect_delete_months"])
            last = func.coalesce(Prospect.last_touch_at, Prospect.created_at)
            stats["archived"] += db.execute(update(Prospect).where(
                Prospect.org_id == org.id, Prospect.archived_at.is_(None),
                last < _now() - timedelta(days=30 * archive_m),
                or_(Prospect.next_action_on.is_(None), Prospect.next_action_on < func.current_date()))
                .values(archived_at=_now())).rowcount
            stats["deleted"] += db.execute(delete(Prospect).where(
                Prospect.org_id == org.id, Prospect.archived_at.is_not(None),
                last < _now() - timedelta(days=30 * delete_m))).rowcount
    return stats
