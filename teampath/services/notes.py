"""Notes from trainings and conferences, and sharing them with the team.

* An **event** (conference, training, webinar) holds **sessions** (talks). Events are
  private to their creator, or a leader's *team event* that everyone in their downline
  can pick, so the whole team's notes land in one place. Admins can create events for
  the whole organization.
* A **note** is private to its author — leaders and admins included. The body is
  encrypted at rest. Lines that start with a marker are picked up as takeaways:

      KEY: …       a key point
      ACTION: …    something I'll do (can become a goal or a task)
      Q: …         a question for the speaker or my leader
      STAT: …      a number or quote to verify before using

* **Sharing** publishes a snapshot (so the author can keep editing privately) to their
  leader, their team (downline, not assistants) and/or specific people in their branch.
  Recipients can comment. Row-level security enforces all of this.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select, text

from .. import audit, events
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import tenant_session
from ..models import (
    Membership,
    Note,
    NoteAction,
    NoteComment,
    NoteEvent,
    NotePhoto,
    NoteSession,
    SharedNote,
    SharedNoteRecipient,
    StoredFile,
    User,
)
from ..security import decrypt, encrypt, sign_file_url
from .notify import after_commit, notify

MARKERS = {
    "key": ("KEY", "Key point"),
    "action": ("ACTION", "Action item"),
    "question": ("Q", "Question"),
    "stat": ("STAT", "Stat or quote"),
}
_ALIASES = {"KEY": "key", "ACTION": "action", "TODO": "action", "Q": "question", "QUESTION": "question",
            "STAT": "stat", "QUOTE": "stat"}
_MARKER_RE = re.compile(r"^\s*(?:[-*•]\s*)?(KEY|ACTION|TODO|QUESTION|Q|STAT|QUOTE)\s*[:\-]\s*(.+?)\s*$", re.I)
DEFAULT_TAGS = ["Skills", "Financial concepts", "Products", "Recruiting", "Mindset", "Compliance"]
DISCLAIMER_TAGS = {"financial concepts", "compliance"}
DISCLAIMER = "Personal notes — not compliance-reviewed."
MAX_BODY = 100_000
PHOTO_EXT = {".png", ".jpg", ".jpeg"}
_UNSET = object()


def _now():
    return datetime.now(timezone.utc)


def _enc(v: str | None) -> str | None:
    v = (v or "").strip("\n")
    return encrypt(v) if v.strip() else None


def _dec(v: str | None) -> str:
    if not v:
        return ""
    try:
        return decrypt(v)
    except Exception:
        return ""


def _tags(values) -> list[str]:
    out: list[str] = []
    for t in values or []:
        t = re.sub(r"\s+", " ", str(t)).strip()[:40]
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:12]


def _snippet(body: str, n: int = 140) -> str:
    plain = " ".join(_MARKER_RE.sub(r"\2", ln).strip() for ln in body.splitlines() if ln.strip())
    return plain[:n] + ("…" if len(plain) > n else "")


def line_key(text_: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text_.strip().lower()).encode()).hexdigest()[:32]


def parse_line(line: str) -> tuple[str, str] | None:
    """Returns (marker kind, text) for a marked line, else None."""
    m = _MARKER_RE.match(line)
    if not m:
        return None
    return _ALIASES[m.group(1).upper()], m.group(2)


def _changed(db, ctx: TenantContext, **kw) -> None:
    after_commit(db, lambda: events.publish(events.org_topic(ctx.org_id), kind="notes",
                                            **{k: str(v) for k, v in kw.items()}))


def _photo_url(ctx: TenantContext, file_id) -> str:
    return sign_file_url(str(file_id), str(ctx.user_id), "orig", ttl=1800)


# ------------------------------------------------------------ events & sessions

@dataclass
class EventRow:
    id: uuid.UUID
    title: str
    starts_on: date | None
    ends_on: date | None
    location: str | None
    organizer: str | None
    team_event: bool
    whole_org: bool
    owner_id: uuid.UUID
    owner_name: str
    my_notes: int = 0

    @property
    def dates(self) -> str:
        if not self.starts_on:
            return ""
        if self.ends_on and self.ends_on != self.starts_on:
            return f"{self.starts_on:%b %d} – {self.ends_on:%b %d, %Y}"
        return f"{self.starts_on:%b %d, %Y}"


@dataclass
class SessionRow:
    id: uuid.UUID
    title: str
    speaker: str | None
    starts_at: str | None
    created_by: uuid.UUID


@dataclass
class EventDetail(EventRow):
    sessions: list[SessionRow] = field(default_factory=list)


def _event_row(e: NoteEvent, owner_name: str, my_notes: int = 0) -> EventRow:
    return EventRow(e.id, e.title, e.starts_on, e.ends_on, e.location, e.organizer, e.team_event,
                    e.team_event and e.scope_path is None, e.owner_id, owner_name, my_notes)


def list_events(ctx: TenantContext) -> list[EventRow]:
    """My own events plus team events from my upline (RLS decides which)."""
    with tenant_session(ctx) as db:
        counts = dict(db.execute(select(Note.event_id, func.count()).where(
            Note.org_id == ctx.org_id, Note.author_id == ctx.user_id, Note.event_id.is_not(None))
            .group_by(Note.event_id)).all())
        rows = db.execute(select(NoteEvent, User.name).join(User, User.id == NoteEvent.owner_id)
                          .where(NoteEvent.org_id == ctx.org_id)
                          .order_by(NoteEvent.starts_on.desc().nulls_last(), NoteEvent.created_at.desc())).all()
        return [_event_row(e, n, int(counts.get(e.id, 0))) for e, n in rows]


def _clean_event(title, starts_on, ends_on, location, organizer):
    title = (title or "").strip()
    if not title:
        raise ValidationError("Name the event.")
    if starts_on and ends_on and ends_on < starts_on:
        raise ValidationError("The event can't end before it starts.")
    return (title[:200], starts_on, ends_on or None, (location or "").strip()[:200] or None,
            (organizer or "").strip()[:200] or None)


def create_event(ctx: TenantContext, title: str, starts_on: date | None = None, ends_on: date | None = None,
                 location: str = "", organizer: str = "", for_team: bool = False,
                 whole_org: bool = False) -> uuid.UUID:
    title, starts_on, ends_on, location, organizer = _clean_event(title, starts_on, ends_on, location, organizer)
    if whole_org and not ctx.is_admin:
        raise PermissionDenied("Only admins can create events for the whole organization.")
    if for_team and not (ctx.is_author or whole_org):
        raise PermissionDenied("Leaders create team events.")
    team = for_team or whole_org
    with tenant_session(ctx) as db:
        e = NoteEvent(org_id=ctx.org_id, owner_id=ctx.user_id, title=title, starts_on=starts_on, ends_on=ends_on,
                      location=location, organizer=organizer, team_event=team,
                      scope_path=None if (whole_org or not team) else ctx.path)
        db.add(e)
        db.flush()
        if team:
            audit.record(db, ctx, "note_event.created", "note_event", e.id, team=True, whole_org=whole_org)
            _changed(db, ctx, event_id=e.id)
        return e.id


def update_event(ctx: TenantContext, event_id: uuid.UUID, title: str, starts_on: date | None = None,
                 ends_on: date | None = None, location: str = "", organizer: str = "") -> None:
    title, starts_on, ends_on, location, organizer = _clean_event(title, starts_on, ends_on, location, organizer)
    with tenant_session(ctx) as db:
        e = db.get(NoteEvent, event_id)
        if not e:
            raise NotFound("Event not found")
        if e.owner_id != ctx.user_id:
            raise PermissionDenied("Only the person who created the event can change it.")
        e.title, e.starts_on, e.ends_on, e.location, e.organizer = title, starts_on, ends_on, location, organizer
        _changed(db, ctx, event_id=e.id)


def delete_event(ctx: TenantContext, event_id: uuid.UUID) -> None:
    """Deletes the event and its sessions. Notes are kept (they just lose the link)."""
    with tenant_session(ctx) as db:
        e = db.get(NoteEvent, event_id)
        if not e:
            raise NotFound("Event not found")
        if e.owner_id != ctx.user_id:
            raise PermissionDenied("Only the person who created the event can delete it.")
        db.delete(e)
        if e.team_event:
            audit.record(db, ctx, "note_event.deleted", "note_event", event_id)
        _changed(db, ctx, event_id=event_id)


def get_event(ctx: TenantContext, event_id: uuid.UUID) -> EventDetail:
    with tenant_session(ctx) as db:
        row = db.execute(select(NoteEvent, User.name).join(User, User.id == NoteEvent.owner_id)
                         .where(NoteEvent.id == event_id)).first()
        if not row:
            raise NotFound("Event not found")
        e, owner = row
        sessions = db.scalars(select(NoteSession).where(NoteSession.event_id == event_id)
                              .order_by(NoteSession.starts_at.nulls_last(), NoteSession.created_at)).all()
        base = _event_row(e, owner)
        return EventDetail(**base.__dict__, sessions=[SessionRow(s.id, s.title, s.speaker, s.starts_at, s.created_by)
                                                      for s in sessions])


def _clean_time(v: str | None) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?$", v, re.I)
    if not m:
        raise ValidationError("Enter a time like 9:30 or 2pm.")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        h = h % 12 + (12 if m.group(3).lower().startswith("p") else 0)
    if h > 23 or mi > 59:
        raise ValidationError("Enter a time like 9:30 or 2pm.")
    return f"{h:02d}:{mi:02d}"


def add_session(ctx: TenantContext, event_id: uuid.UUID, title: str, speaker: str = "",
                starts_at: str | None = None) -> uuid.UUID:
    """Anyone who can see the event can add a session to it (attendees build the agenda together)."""
    title = (title or "").strip()
    if not title:
        raise ValidationError("Name the session.")
    with tenant_session(ctx) as db:
        if not db.get(NoteEvent, event_id):
            raise NotFound("Event not found")
        s = NoteSession(org_id=ctx.org_id, event_id=event_id, title=title[:200],
                        speaker=(speaker or "").strip()[:200] or None, starts_at=_clean_time(starts_at),
                        created_by=ctx.user_id)
        db.add(s)
        db.flush()
        _changed(db, ctx, event_id=event_id)
        return s.id


def update_session(ctx: TenantContext, session_id: uuid.UUID, title: str, speaker: str = "",
                   starts_at: str | None = None) -> None:
    title = (title or "").strip()
    if not title:
        raise ValidationError("Name the session.")
    with tenant_session(ctx) as db:
        s = db.get(NoteSession, session_id)
        if not s:
            raise NotFound("Session not found")
        e = db.get(NoteEvent, s.event_id)
        if s.created_by != ctx.user_id and e.owner_id != ctx.user_id:
            raise PermissionDenied("Only whoever added the session (or the event's creator) can change it.")
        s.title, s.speaker, s.starts_at = title[:200], (speaker or "").strip()[:200] or None, _clean_time(starts_at)
        _changed(db, ctx, event_id=s.event_id)


def delete_session(ctx: TenantContext, session_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        s = db.get(NoteSession, session_id)
        if not s:
            raise NotFound("Session not found")
        e = db.get(NoteEvent, s.event_id)
        if s.created_by != ctx.user_id and e.owner_id != ctx.user_id:
            raise PermissionDenied("Only whoever added the session (or the event's creator) can delete it.")
        db.delete(s)
        _changed(db, ctx, event_id=s.event_id)


# ------------------------------------------------------------ notes

@dataclass
class NoteRow:
    id: uuid.UUID
    title: str
    event_id: uuid.UUID | None
    event_title: str | None
    session_id: uuid.UUID | None
    session_title: str | None
    speaker: str | None
    tags: list[str]
    updated_at: datetime
    snippet: str
    shared: bool
    photos: int
    markers: dict[str, int]


@dataclass
class Photo:
    id: uuid.UUID
    file_id: uuid.UUID
    url: str
    caption: str | None


@dataclass
class ShareInfo:
    id: uuid.UUID
    to_leader: bool
    to_team: bool
    people: list[tuple[uuid.UUID, str]]
    updated_at: datetime
    out_of_date: bool


@dataclass
class NoteDetail:
    id: uuid.UUID
    title: str
    body: str
    tags: list[str]
    event_id: uuid.UUID | None
    event_title: str | None
    session_id: uuid.UUID | None
    session_title: str | None
    speaker: str | None
    updated_at: datetime
    created_at: datetime
    photos: list[Photo]
    share: ShareInfo | None


def _count_markers(body: str) -> dict[str, int]:
    out = {k: 0 for k in MARKERS}
    for ln in body.splitlines():
        p = parse_line(ln)
        if p:
            out[p[0]] += 1
    return out


def _own_note(db, ctx: TenantContext, note_id) -> Note:
    n = db.get(Note, note_id)  # RLS: author only
    if not n or n.author_id != ctx.user_id:
        raise NotFound("Note not found")
    return n


def _check_links(db, event_id, session_id) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    if session_id:
        s = db.get(NoteSession, session_id)
        if not s:
            raise NotFound("Session not found")
        event_id = s.event_id
    if event_id and not db.get(NoteEvent, event_id):
        raise NotFound("Event not found")
    return event_id, session_id


def create_note(ctx: TenantContext, title: str = "", event_id: uuid.UUID | None = None,
                session_id: uuid.UUID | None = None, body: str = "", tags: list[str] | None = None) -> uuid.UUID:
    if len(body or "") > MAX_BODY:
        raise ValidationError("That note is too long.")
    with tenant_session(ctx) as db:
        event_id, session_id = _check_links(db, event_id, session_id)
        title = (title or "").strip()
        if not title:
            s = db.get(NoteSession, session_id) if session_id else None
            e = db.get(NoteEvent, event_id) if event_id else None
            title = s.title if s else e.title if e else f"Notes {ctx.today():%b %d}"
        n = Note(org_id=ctx.org_id, author_id=ctx.user_id, event_id=event_id, session_id=session_id,
                 title=title[:200], body_enc=_enc(body), tags=_tags(tags))
        db.add(n)
        db.flush()
        return n.id


def save_note(ctx: TenantContext, note_id: uuid.UUID, *, title=_UNSET, body=_UNSET, tags=_UNSET,
              event_id=_UNSET, session_id=_UNSET) -> datetime:
    """Saves changes (used by autosave). Returns the new updated time."""
    with tenant_session(ctx) as db:
        n = _own_note(db, ctx, note_id)
        if title is not _UNSET:
            t = (title or "").strip()
            if not t:
                raise ValidationError("Give the note a title.")
            n.title = t[:200]
        if body is not _UNSET:
            if len(body or "") > MAX_BODY:
                raise ValidationError("That note is too long.")
            n.body_enc = _enc(body)
        if tags is not _UNSET:
            n.tags = _tags(tags)
        if session_id is not _UNSET and session_id:
            n.event_id, n.session_id = _check_links(db, None, session_id)
        elif event_id is not _UNSET or session_id is not _UNSET:
            ev = n.event_id if event_id is _UNSET else event_id
            # Moving to another event (or none) clears the session.
            se = n.session_id if (session_id is _UNSET and ev == n.event_id) else None
            n.event_id, n.session_id = _check_links(db, ev, se)
        n.updated_at = _now()
        return n.updated_at


def delete_note(ctx: TenantContext, note_id: uuid.UUID) -> None:
    """Deletes the note, its photos and any shared copy."""
    with tenant_session(ctx) as db:
        n = _own_note(db, ctx, note_id)
        file_ids = list(db.scalars(select(NotePhoto.file_id).where(NotePhoto.note_id == n.id)))
        was_shared = db.scalar(select(SharedNote.id).where(SharedNote.note_id == n.id)) is not None
        db.delete(n)
        db.flush()
        _delete_files(db, ctx, file_ids)
        if was_shared:
            audit.record(db, ctx, "note.unshared", "note", note_id, deleted=True)
            _changed(db, ctx, note_id=note_id)


def _delete_files(db, ctx, file_ids) -> None:
    from .. import storage

    for fid in file_ids:
        still = db.scalar(select(func.count()).select_from(NotePhoto).where(NotePhoto.file_id == fid))
        if still:
            continue
        f = db.get(StoredFile, fid)
        if f and f.owner_id == ctx.user_id:
            try:
                storage.absolute(f.storage_path).unlink(missing_ok=True)
            except Exception:
                pass
            db.delete(f)


def _labels(db, notes: list[Note]) -> tuple[dict, dict]:
    ev_ids = {n.event_id for n in notes if n.event_id}
    se_ids = {n.session_id for n in notes if n.session_id}
    evs = {e.id: e.title for e in db.scalars(select(NoteEvent).where(NoteEvent.id.in_(ev_ids)))} if ev_ids else {}
    ses = {s.id: (s.title, s.speaker) for s in db.scalars(select(NoteSession).where(NoteSession.id.in_(se_ids)))} \
        if se_ids else {}
    return evs, ses


def list_notes(ctx: TenantContext, search: str = "", event_id: uuid.UUID | None = None,
               tag: str | None = None) -> list[NoteRow]:
    """My notes, newest first. Search covers title, text, tags, speaker and event."""
    with tenant_session(ctx) as db:
        q = select(Note).where(Note.org_id == ctx.org_id, Note.author_id == ctx.user_id)
        if event_id:
            q = q.where(Note.event_id == event_id)
        notes = list(db.scalars(q.order_by(Note.updated_at.desc())))
        evs, ses = _labels(db, notes)
        shared = set(db.scalars(select(SharedNote.note_id).where(SharedNote.author_id == ctx.user_id)))
        photos = dict(db.execute(select(NotePhoto.note_id, func.count()).where(NotePhoto.author_id == ctx.user_id)
                                 .group_by(NotePhoto.note_id)).all())
        out = []
        needle = (search or "").strip().lower()
        for n in notes:
            body = _dec(n.body_enc)
            st, sp = ses.get(n.session_id, (None, None))
            et = evs.get(n.event_id)
            if tag and tag.lower() not in {t.lower() for t in n.tags}:
                continue
            if needle and needle not in " ".join([n.title, body, " ".join(n.tags), st or "", sp or "",
                                                  et or ""]).lower():
                continue
            out.append(NoteRow(n.id, n.title, n.event_id, et, n.session_id, st, sp, list(n.tags), n.updated_at,
                               _snippet(body), n.id in shared, int(photos.get(n.id, 0)), _count_markers(body)))
        return out


def get_note(ctx: TenantContext, note_id: uuid.UUID) -> NoteDetail:
    with tenant_session(ctx) as db:
        n = _own_note(db, ctx, note_id)
        evs, ses = _labels(db, [n])
        st, sp = ses.get(n.session_id, (None, None))
        photos = [Photo(p.id, p.file_id, _photo_url(ctx, p.file_id), p.caption) for p in db.scalars(
            select(NotePhoto).where(NotePhoto.note_id == n.id).order_by(NotePhoto.created_at))]
        share = None
        s = db.scalar(select(SharedNote).where(SharedNote.note_id == n.id))
        if s:
            recips = db.execute(select(SharedNoteRecipient.user_id, User.name, SharedNoteRecipient.is_leader)
                                .join(User, User.id == SharedNoteRecipient.user_id)
                                .where(SharedNoteRecipient.shared_id == s.id).order_by(User.name)).all()
            share = ShareInfo(s.id, any(r[2] for r in recips), s.to_team,
                              [(u, nm) for u, nm, lead in recips if not lead], s.updated_at,
                              n.updated_at > s.updated_at)
        return NoteDetail(n.id, n.title, _dec(n.body_enc), list(n.tags), n.event_id, evs.get(n.event_id),
                          n.session_id, st, sp, n.updated_at, n.created_at, photos, share)


def _reencode_image(src: Path) -> Path:
    """Decodes and re-encodes a photo: fixes rotation, drops EXIF (including GPS location)
    and anything appended to the file, and caps the size."""
    from PIL import Image, ImageOps

    try:
        with Image.open(src) as im:
            im.verify()
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            im.thumbnail((2000, 2000))
            dest = src.with_name(src.stem + "_clean.jpg")
            im.save(dest, format="JPEG", quality=85, optimize=True)
    except Exception:
        raise ValidationError("That doesn't look like a photo we can read (use JPG or PNG).")
    finally:
        src.unlink(missing_ok=True)
    return dest


def add_photo(ctx: TenantContext, note_id: uuid.UUID, tmp_path: Path, original_name: str,
              caption: str = "") -> uuid.UUID:
    from .. import storage
    from .orgs import save_upload

    with tenant_session(ctx) as db:
        _own_note(db, ctx, note_id)
    ext, _ = storage.validate_upload(tmp_path, original_name, PHOTO_EXT)
    clean = _reencode_image(tmp_path)
    fid = save_upload(ctx, clean, Path(original_name).stem[:100] + ".jpg", "note_photo", PHOTO_EXT)
    with tenant_session(ctx) as db:
        n = _own_note(db, ctx, note_id)
        p = NotePhoto(org_id=ctx.org_id, note_id=n.id, author_id=ctx.user_id, file_id=fid,
                      caption=(caption or "").strip()[:200] or None)
        db.add(p)
        n.updated_at = _now()
        db.flush()
        return p.id


def remove_photo(ctx: TenantContext, photo_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        p = db.get(NotePhoto, photo_id)
        if not p or p.author_id != ctx.user_id:
            raise NotFound("Photo not found")
        fid = p.file_id
        n = db.get(Note, p.note_id)
        db.delete(p)
        db.flush()
        n.updated_at = _now()
        # Keep the file while a shared copy still shows it.
        in_share = db.scalar(select(func.count()).select_from(SharedNote).where(
            SharedNote.author_id == ctx.user_id, SharedNote.photo_ids.any(fid)))
        if not in_share:
            _delete_files(db, ctx, [fid])


# ------------------------------------------------------------ takeaways

@dataclass
class Takeaway:
    note_id: uuid.UUID
    note_title: str
    session_title: str | None
    event_title: str | None
    kind: str
    text: str
    key: str
    made: str | None = None  # goal | task
    target_id: uuid.UUID | None = None


def takeaways(ctx: TenantContext, event_id: uuid.UUID | None = None) -> list[Takeaway]:
    """Every marked line in my notes (optionally for one event), in note order."""
    with tenant_session(ctx) as db:
        q = select(Note).where(Note.org_id == ctx.org_id, Note.author_id == ctx.user_id)
        if event_id:
            q = q.where(Note.event_id == event_id)
        notes = list(db.scalars(q.order_by(Note.created_at)))
        evs, ses = _labels(db, notes)
        made = {(a.note_id, a.line_key): (a.kind, a.target_id) for a in db.scalars(
            select(NoteAction).where(NoteAction.author_id == ctx.user_id))}
        out = []
        for n in notes:
            seen = set()
            for ln in _dec(n.body_enc).splitlines():
                p = parse_line(ln)
                if not p:
                    continue
                k = line_key(p[1])
                if (p[0], k) in seen:
                    continue
                seen.add((p[0], k))
                m = made.get((n.id, k), (None, None))
                out.append(Takeaway(n.id, n.title, ses.get(n.session_id, (None, None))[0], evs.get(n.event_id),
                                    p[0], p[1], k, m[0], m[1]))
        return out


def _find_action(db, ctx, note_id, key) -> tuple[Note, str]:
    n = _own_note(db, ctx, note_id)
    for ln in _dec(n.body_enc).splitlines():
        p = parse_line(ln)
        if p and p[0] == "action" and line_key(p[1]) == key:
            return n, p[1]
    raise NotFound("That action item is no longer in the note.")


def make_goal(ctx: TenantContext, note_id: uuid.UUID, key: str, horizon: str = "30") -> uuid.UUID:
    """Turns an action item into a goal (proposed to the leader, like any goal you set yourself)."""
    from . import goals

    with tenant_session(ctx) as db:
        n, txt = _find_action(db, ctx, note_id, key)
        if db.scalar(select(NoteAction.id).where(NoteAction.note_id == n.id, NoteAction.line_key == key)):
            raise ValidationError("That action item already became a goal or task.")
        detail = f"From my notes: {n.title}"
    gid = goals.create_goal(ctx, txt, detail, horizon)
    with tenant_session(ctx) as db:
        db.add(NoteAction(org_id=ctx.org_id, note_id=note_id, author_id=ctx.user_id, line_key=key, kind="goal",
                          target_id=gid))
    return gid


def make_task(ctx: TenantContext, note_id: uuid.UUID, key: str, assignee_id: uuid.UUID | None = None,
              due_on: date | None = None) -> uuid.UUID:
    """Turns an action item into a task for me (or one of my assistants)."""
    from . import tasks

    with tenant_session(ctx) as db:
        n, txt = _find_action(db, ctx, note_id, key)
        if db.scalar(select(NoteAction.id).where(NoteAction.note_id == n.id, NoteAction.line_key == key)):
            raise ValidationError("That action item already became a goal or task.")
        detail = f"From my notes: {n.title}"
    tid = tasks.create(ctx, title=txt, details=detail, assignee_id=assignee_id, due_on=due_on)
    with tenant_session(ctx) as db:
        db.add(NoteAction(org_id=ctx.org_id, note_id=note_id, author_id=ctx.user_id, line_key=key, kind="task",
                          target_id=tid))
    return tid


# ------------------------------------------------------------ sharing

def share_targets(ctx: TenantContext) -> tuple[object | None, bool, list]:
    """(my leader or None, whether I have a team, people I can share with individually)."""
    from .orgs import list_people, my_leader

    leader = my_leader(ctx)
    people = [p for p in list_people(ctx, include_upline=True)
              if p.user_id != ctx.user_id and p.status == "active"]
    has_team = any(p.path.startswith(ctx.path + ".") and p.role != "assistant" for p in people)
    return leader, has_team, people


def share(ctx: TenantContext, note_id: uuid.UUID, *, to_leader: bool = False, to_team: bool = False,
          people: list[uuid.UUID] | None = None) -> uuid.UUID:
    """Shares (or re-shares) a snapshot of the note. Replaces the previous audience."""
    leader, _, allowed = share_targets(ctx)
    allowed_ids = {p.user_id for p in allowed}
    people = [p for p in dict.fromkeys(people or []) if not (leader and p == leader.user_id and to_leader)]
    if any(p not in allowed_ids for p in people):
        raise PermissionDenied("You can share with people in your upline and your team.")
    if to_leader and not leader:
        raise ValidationError("You don't have a leader above you to share with.")
    if not (to_leader or to_team or people):
        raise ValidationError("Choose who to share with.")
    with tenant_session(ctx) as db:
        n = _own_note(db, ctx, note_id)
        evs, ses = _labels(db, [n])
        st, sp = ses.get(n.session_id, (None, None))
        photo_ids = list(db.scalars(select(NotePhoto.file_id).where(NotePhoto.note_id == n.id)
                                    .order_by(NotePhoto.created_at)))
        s = db.scalar(select(SharedNote).where(SharedNote.note_id == n.id))
        before = set()
        if s:
            before = set(db.scalars(select(SharedNoteRecipient.user_id).where(SharedNoteRecipient.shared_id == s.id)))
            had_team = s.to_team
            db.execute(delete(SharedNoteRecipient).where(SharedNoteRecipient.shared_id == s.id))
        else:
            had_team = False
            s = SharedNote(org_id=ctx.org_id, note_id=n.id, author_id=ctx.user_id, author_path=ctx.path)
            db.add(s)
        s.author_path = ctx.path
        s.to_team = to_team
        s.title, s.body_enc, s.tags = n.title, n.body_enc, list(n.tags)
        s.event_id, s.event_title, s.session_title, s.speaker = n.event_id, evs.get(n.event_id), st, sp
        s.photo_ids = photo_ids
        s.updated_at = _now()
        db.flush()
        recipients = ([(leader.user_id, True)] if to_leader else []) + [(p, False) for p in people]
        for uid, is_leader in recipients:
            db.add(SharedNoteRecipient(org_id=ctx.org_id, shared_id=s.id, author_id=ctx.user_id, user_id=uid,
                                       is_leader=is_leader))
        # Tell the people newly added (and, the first time, the author's direct team).
        to_tell = [uid for uid, _ in recipients if uid not in before]
        if to_team and not had_team:
            to_tell += list(db.scalars(select(Membership.user_id).where(
                Membership.org_id == ctx.org_id, Membership.status == "active", Membership.role != "assistant",
                text("memberships.path ~ cast(:q as lquery)").bindparams(q=ctx.path + ".*{1}"))))
        for uid in dict.fromkeys(to_tell):
            if uid != ctx.user_id:
                notify(db, ctx, uid, "note_shared", f"{ctx.user_name} shared notes: {n.title[:100]}",
                       body=evs.get(n.event_id), route=f"/notes/shared/{s.id}")
        audit.record(db, ctx, "note.shared", "note", n.id, to_leader=to_leader, to_team=to_team, people=len(people))
        _changed(db, ctx, shared_id=s.id)
        return s.id


def refresh_share(ctx: TenantContext, note_id: uuid.UUID) -> uuid.UUID:
    """Updates the shared copy with the note's current text, keeping the same audience."""
    with tenant_session(ctx) as db:
        n = _own_note(db, ctx, note_id)
        s = db.scalar(select(SharedNote).where(SharedNote.note_id == n.id))
        if not s:
            raise ValidationError("This note isn't shared.")
        rec = db.execute(select(SharedNoteRecipient.user_id, SharedNoteRecipient.is_leader)
                         .where(SharedNoteRecipient.shared_id == s.id)).all()
        to_team = s.to_team
    return share(ctx, note_id, to_leader=any(lead for _, lead in rec), to_team=to_team,
                 people=[u for u, lead in rec if not lead])


def unshare(ctx: TenantContext, note_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        n = _own_note(db, ctx, note_id)
        s = db.scalar(select(SharedNote).where(SharedNote.note_id == n.id))
        if not s:
            return
        photo_ids = list(s.photo_ids or [])
        db.delete(s)
        db.flush()
        current = set(db.scalars(select(NotePhoto.file_id).where(NotePhoto.note_id == n.id)))
        _delete_files(db, ctx, [f for f in photo_ids if f not in current])
        audit.record(db, ctx, "note.unshared", "note", n.id)
        _changed(db, ctx, note_id=n.id)


@dataclass
class SharedRow:
    id: uuid.UUID
    note_id: uuid.UUID
    author_id: uuid.UUID
    author_name: str
    title: str
    event_id: uuid.UUID | None
    event_title: str | None
    session_title: str | None
    speaker: str | None
    tags: list[str]
    updated_at: datetime
    snippet: str
    comments: int
    how: str  # "you" | "team" | "mine"

    @property
    def group_key(self) -> str:
        return str(self.event_id) if self.event_id else (self.event_title or "").strip().lower()


@dataclass
class CommentRow:
    id: uuid.UUID
    author_id: uuid.UUID
    author_name: str
    body: str
    created_at: datetime


@dataclass
class SharedDetail(SharedRow):
    body: str = ""
    photos: list[str] = field(default_factory=list)
    thread: list[CommentRow] = field(default_factory=list)
    disclaimer: str | None = None
    audience: str = ""


def _shared_rows(db, ctx, q) -> list[SharedRow]:
    rows = db.execute(q).all()
    ids = [s.id for s, _ in rows]
    counts = dict(db.execute(select(NoteComment.shared_id, func.count()).where(NoteComment.shared_id.in_(ids))
                             .group_by(NoteComment.shared_id)).all()) if ids else {}
    mine_direct = set(db.scalars(select(SharedNoteRecipient.shared_id).where(
        SharedNoteRecipient.user_id == ctx.user_id, SharedNoteRecipient.shared_id.in_(ids)))) if ids else set()
    out = []
    for s, name in rows:
        how = "mine" if s.author_id == ctx.user_id else ("you" if s.id in mine_direct else "team")
        out.append(SharedRow(s.id, s.note_id, s.author_id, name, s.title, s.event_id, s.event_title, s.session_title,
                             s.speaker, list(s.tags or []), s.updated_at, _snippet(_dec(s.body_enc)),
                             int(counts.get(s.id, 0)), how))
    return out


def shared_with_me(ctx: TenantContext, event_id: uuid.UUID | None = None) -> list[SharedRow]:
    """Notes others shared with me or my team, newest first (RLS decides which)."""
    with tenant_session(ctx) as db:
        q = (select(SharedNote, User.name).join(User, User.id == SharedNote.author_id)
             .where(SharedNote.org_id == ctx.org_id, SharedNote.author_id != ctx.user_id))
        if event_id:
            q = q.where(SharedNote.event_id == event_id)
        return _shared_rows(db, ctx, q.order_by(SharedNote.updated_at.desc()))


def my_shared(ctx: TenantContext) -> list[SharedRow]:
    with tenant_session(ctx) as db:
        q = (select(SharedNote, User.name).join(User, User.id == SharedNote.author_id)
             .where(SharedNote.org_id == ctx.org_id, SharedNote.author_id == ctx.user_id))
        return _shared_rows(db, ctx, q.order_by(SharedNote.updated_at.desc()))


def get_shared(ctx: TenantContext, shared_id: uuid.UUID) -> SharedDetail:
    with tenant_session(ctx) as db:
        row = db.execute(select(SharedNote, User.name).join(User, User.id == SharedNote.author_id)
                         .where(SharedNote.id == shared_id)).first()
        if not row:
            raise NotFound("These notes aren't shared with you.")
        base = _shared_rows(db, ctx, select(SharedNote, User.name).join(User, User.id == SharedNote.author_id)
                            .where(SharedNote.id == shared_id))[0]
        s = row[0]
        rec = db.execute(select(User.name, SharedNoteRecipient.is_leader).join(
            User, User.id == SharedNoteRecipient.user_id).where(SharedNoteRecipient.shared_id == s.id)).all()
        parts = (["their leader"] if any(lead for _, lead in rec) else []) + (["their team"] if s.to_team else []) + \
            sorted(n for n, lead in rec if not lead)
        wide = s.to_team or any(not lead for _, lead in rec)
        disclaimer = DISCLAIMER if wide and DISCLAIMER_TAGS & {t.lower() for t in (s.tags or [])} else None
        thread = [CommentRow(c.id, c.author_id, n, _dec(c.body_enc), c.created_at) for c, n in db.execute(
            select(NoteComment, User.name).join(User, User.id == NoteComment.author_id)
            .where(NoteComment.shared_id == s.id).order_by(NoteComment.created_at)).all()]
        photos = [_photo_url(ctx, f) for f in (s.photo_ids or [])]
        return SharedDetail(**base.__dict__, body=_dec(s.body_enc), photos=photos, thread=thread,
                            disclaimer=disclaimer, audience=", ".join(parts))


def comment(ctx: TenantContext, shared_id: uuid.UUID, body: str) -> uuid.UUID:
    body = (body or "").strip()
    if not body:
        raise ValidationError("Write a comment.")
    if len(body) > 4000:
        raise ValidationError("That comment is too long.")
    with tenant_session(ctx) as db:
        s = db.get(SharedNote, shared_id)  # RLS: only people it's shared with
        if not s:
            raise NotFound("These notes aren't shared with you.")
        c = NoteComment(org_id=ctx.org_id, shared_id=s.id, author_id=ctx.user_id, body_enc=encrypt(body))
        db.add(c)
        db.flush()
        others = set(db.scalars(select(NoteComment.author_id).where(NoteComment.shared_id == s.id))) | {s.author_id}
        others.discard(ctx.user_id)
        for uid in others:
            what = "your notes" if uid == s.author_id else "notes"
            notify(db, ctx, uid, "note_comment", f"{ctx.user_name} commented on {what}: {s.title[:80]}",
                   body=body[:200], route=f"/notes/shared/{s.id}")
        _changed(db, ctx, shared_id=s.id)
        return c.id


def delete_comment(ctx: TenantContext, comment_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        c = db.get(NoteComment, comment_id)
        if not c or c.author_id != ctx.user_id:
            raise NotFound("Comment not found")
        sid = c.shared_id
        db.delete(c)
        _changed(db, ctx, shared_id=sid)


def org_tags(settings: dict | None) -> list[str]:
    return _tags((settings or {}).get("note_tags") or DEFAULT_TAGS)
