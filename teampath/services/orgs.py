"""Organizations, branding, hierarchy (upline/downline) and invitations."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text, update

from .. import audit, jobs, mailer
from ..config import get_settings
from ..context import ADMIN_ROLES, ROLES, NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import tenant_session
from ..models import ActivityType, Invitation, Membership, OrgBranding, Organization, StoredFile, User
from ..security import new_token, token_hash
from .. import storage

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_SETTINGS = {
    "activity_visibility": "auto",  # auto: upline sees daily activity | shared: only via updates
    "mfa_required_roles": ["owner", "admin", "leader"],
    "allow_team_branding": True,
    "leaderboards": False,
    "invite_expiry_days": 7,
    # Agents build their own teams: members may recruit into their own downline
    # and become leaders when their first recruit joins.
    "members_can_recruit": True,
    # Agents can invite assistants and delegate tasks to them
    "allow_assistants": True,
    # Prospects (agent-owned): archive after no activity, delete later
    "prospect_archive_months": 12,
    "prospect_delete_months": 24,
    # Topic tags offered when taking notes (agents can add their own)
    "note_tags": ["Skills", "Financial concepts", "Products", "Recruiting", "Mindset", "Compliance"],
}

ACTIVITY_TEMPLATES = {
    "generic": [("tasks", "Tasks completed", 5), ("learning", "Learning (30 min)", 1), ("meetings", "Meetings", 1)],
    "insurance": [
        ("dials", "Dials", 40), ("contacts", "Contacts", 15), ("appts_set", "Appointments set", 3),
        ("appts_held", "Appointments held", 2), ("apps", "Applications submitted", 1),
        ("recruiting", "Recruiting conversations", 2), ("training", "Training (30 min)", 1),
    ],
}


def _now():
    return datetime.now(timezone.utc)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:50] or "org"


def _label(membership_id: uuid.UUID) -> str:
    return "m" + membership_id.hex


# ------------------------------------------------------------ records (used inside a session)

def create_org_record(db, *, name: str, org_type: str, status: str) -> Organization:
    name = (name or "").strip()
    if len(name) < 2:
        raise ValidationError("Enter the organization name.")
    if org_type not in ACTIVITY_TEMPLATES:
        raise ValidationError("Unknown organization type.")
    base = slugify(name)
    slug, n = base, 1
    while db.scalar(select(Organization.id).where(Organization.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    org = Organization(name=name, slug=slug, org_type=org_type, status=status, settings=dict(DEFAULT_SETTINGS))
    db.add(org)
    db.flush()
    db.add(OrgBranding(org_id=org.id))
    for i, (key, label, target) in enumerate(ACTIVITY_TEMPLATES[org_type]):
        db.add(ActivityType(org_id=org.id, key=key, label=label, daily_target=target, position=i))
    return org


def add_root_membership(db, org_id, user_id, role="owner") -> Membership:
    return add_membership(db, org_id, user_id, role=role, parent=None)


def add_membership(db, org_id, user_id, role: str, parent: Membership | None) -> Membership:
    if role not in ROLES:
        raise ValidationError("Unknown role.")
    mid = uuid.uuid4()
    path = f"{parent.path}.{_label(mid)}" if parent else _label(mid)
    m = Membership(id=mid, org_id=org_id, user_id=user_id, role=role, parent_id=parent.id if parent else None, path=path)
    db.add(m)
    if role == "assistant" and parent is not None:
        # The assistant works for the agent who invited them.
        from ..models import AssistantLink

        db.add(AssistantLink(org_id=org_id, agent_id=parent.user_id, assistant_id=user_id))
    elif parent is not None and parent.role == "member":
        # First recruit: the inviter now leads a team.
        parent.role = "leader"
        audit.record(db, None, "membership.promoted_leader", "membership", parent.id, org_id=org_id,
                     actor_id=user_id)
    db.flush()
    return m


# ------------------------------------------------------------ organization profile & branding

@dataclass
class OrgProfile:
    id: uuid.UUID
    name: str
    slug: str
    org_type: str
    status: str
    timezone: str
    website: str | None
    address: str | None
    contact_email: str | None
    phone: str | None
    settings: dict
    primary_color: str
    tagline: str | None
    welcome_text: str | None
    logo_file_id: uuid.UUID | None
    team_name: str | None = None
    team_logo_file_id: uuid.UUID | None = None


def get_profile(ctx: TenantContext) -> OrgProfile:
    with tenant_session(ctx) as db:
        org = db.get(Organization, ctx.org_id)
        b = db.get(OrgBranding, ctx.org_id) or OrgBranding(org_id=ctx.org_id)
        # Team branding: the nearest leader above (or at) this user with a team name.
        team = db.execute(
            select(Membership.team_name, Membership.team_logo_file_id)
            .where(Membership.org_id == ctx.org_id, Membership.team_name.is_not(None),
                   text("memberships.path @> cast(:p as ltree)").bindparams(p=ctx.path))
            .order_by(func.nlevel(Membership.path).desc()).limit(1)
        ).first()
        settings = {**DEFAULT_SETTINGS, **(org.settings or {})}
        allow_team = settings.get("allow_team_branding", True)
        return OrgProfile(
            org.id, org.name, org.slug, org.org_type, org.status, org.timezone, org.website, org.address,
            org.contact_email, org.phone, settings, b.primary_color or "#3355CC", b.tagline,
            b.welcome_text, b.logo_file_id,
            team[0] if team and allow_team else None, team[1] if team and allow_team else None,
        )


def update_profile(ctx: TenantContext, **fields) -> None:
    ctx.require_admin()
    allowed = {"name", "timezone", "website", "address", "contact_email", "phone"}
    with tenant_session(ctx) as db:
        org = db.get(Organization, ctx.org_id)
        for k, v in fields.items():
            if k not in allowed:
                raise ValidationError(f"Unknown field {k}")
            v = (v or "").strip() or None
            if k == "name" and not v:
                raise ValidationError("Name is required.")
            if k == "timezone" and v:
                from zoneinfo import ZoneInfo

                try:
                    ZoneInfo(v)
                except Exception:
                    raise ValidationError("Use a time zone like America/New_York.")
            if k == "website" and v and not v.startswith(("https://", "http://")):
                raise ValidationError("Website must start with https://")
            setattr(org, k, v)
        audit.record(db, ctx, "org.profile_updated", "organization", ctx.org_id, fields=sorted(fields))


def update_branding(ctx: TenantContext, *, primary_color: str | None = None, tagline: str | None = None,
                    welcome_text: str | None = None) -> None:
    ctx.require_admin()
    with tenant_session(ctx) as db:
        b = db.get(OrgBranding, ctx.org_id)
        if primary_color is not None:
            if not HEX_COLOR.match(primary_color):
                raise ValidationError("Color must look like #3355CC.")
            b.primary_color = primary_color.upper()
        if tagline is not None:
            b.tagline = tagline.strip()[:200] or None
        if welcome_text is not None:
            b.welcome_text = welcome_text.strip() or None
        audit.record(db, ctx, "org.branding_updated", "organization", ctx.org_id)


def update_settings(ctx: TenantContext, **values) -> None:
    ctx.require_admin()
    with tenant_session(ctx) as db:
        org = db.get(Organization, ctx.org_id)
        s = {**DEFAULT_SETTINGS, **(org.settings or {})}
        for k, v in values.items():
            if k not in DEFAULT_SETTINGS:
                raise ValidationError(f"Unknown setting {k}")
            if k == "activity_visibility" and v not in ("auto", "shared"):
                raise ValidationError("Invalid visibility")
            if k in ("prospect_archive_months", "prospect_delete_months"):
                v = int(v)
                if not 1 <= v <= 120:
                    raise ValidationError("Choose between 1 and 120 months.")
            if k == "note_tags":
                v = [str(t).strip()[:40] for t in v if str(t).strip()][:20]
            if k == "mfa_required_roles" and not set(v) <= set(ROLES):
                raise ValidationError("Invalid roles")
            s[k] = v
        org.settings = s
        audit.record(db, ctx, "org.settings_updated", "organization", ctx.org_id, settings=values)


def save_upload(ctx: TenantContext, tmp_path: Path, original_name: str, purpose: str,
                allowed: set[str] | None = None) -> uuid.UUID:
    """Validates an uploaded file, scans it and moves it to storage.

    Scanning happens right away so the file is usable immediately; only if the
    virus scanner is unreachable is the scan handed to the background worker."""
    from .. import previews

    ext, mime = storage.validate_upload(tmp_path, original_name, allowed)
    fid = uuid.uuid4()
    digest = storage.sha256_of(tmp_path)
    size = tmp_path.stat().st_size
    status = previews.scan_now(tmp_path)
    if status == "infected":
        tmp_path.unlink(missing_ok=True)
        with tenant_session(ctx) as db:
            audit.record(db, ctx, "file.infected", "file", fid, name=original_name, size=size)
        raise ValidationError("This file was blocked by the virus scanner.")
    rel = storage.store(tmp_path, ctx.org_id, fid, ext)
    with tenant_session(ctx) as db:
        db.add(StoredFile(id=fid, org_id=ctx.org_id, owner_id=ctx.user_id, purpose=purpose,
                          original_name=Path(original_name).name[:255], mime=mime, size=size, sha256=digest,
                          storage_path=rel, scan_status=status))
        db.flush()
        if status == "pending":
            jobs.enqueue(db, "scan_file", file_id=fid, org_id=ctx.org_id)
        audit.record(db, ctx, "file.uploaded", "file", fid, purpose=purpose, name=original_name, size=size)
    return fid


def set_logo(ctx: TenantContext, tmp_path: Path, original_name: str) -> None:
    ctx.require_admin()
    fid = save_upload(ctx, tmp_path, original_name, "logo", storage.LOGO_EXT)
    with tenant_session(ctx) as db:
        db.get(OrgBranding, ctx.org_id).logo_file_id = fid
        jobs.enqueue(db, "process_logo", file_id=fid, org_id=ctx.org_id)


def set_team_branding(ctx: TenantContext, team_name: str | None, tmp_path: Path | None = None,
                      original_name: str = "") -> None:
    """A leader names (and optionally brands) their own team."""
    ctx.require("owner", "admin", "leader")
    fid = save_upload(ctx, tmp_path, original_name, "logo", storage.LOGO_EXT) if tmp_path else None
    with tenant_session(ctx) as db:
        org = db.get(Organization, ctx.org_id)
        if not (org.settings or {}).get("allow_team_branding", True):
            raise PermissionDenied("Team branding is turned off for this organization.")
        m = db.get(Membership, ctx.membership_id)
        db.execute(update(Membership).where(Membership.id == m.id).values(
            team_name=(team_name or "").strip()[:120] or None,
            **({"team_logo_file_id": fid} if fid else {})))
        if fid:
            jobs.enqueue(db, "process_logo", file_id=fid, org_id=ctx.org_id)
        audit.record(db, ctx, "team.branding_updated", "membership", m.id)


# ------------------------------------------------------------ people & hierarchy

@dataclass
class Person:
    membership_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    email: str
    role: str
    title: str | None
    team_name: str | None
    path: str
    parent_membership_id: uuid.UUID | None
    depth: int
    status: str
    joined_at: datetime


def list_people(ctx: TenantContext, include_upline: bool = False) -> list[Person]:
    """Everyone this user may see: their branch (admins: everyone). RLS enforces it too."""
    with tenant_session(ctx) as db:
        q = (select(Membership, User.name, User.email).join(User, User.id == Membership.user_id)
             .where(Membership.org_id == ctx.org_id, Membership.status != "left"))
        if not ctx.is_admin:
            cond = "memberships.path <@ cast(:p as ltree)"
            if include_upline:
                cond += " or memberships.path @> cast(:p as ltree)"
            q = q.where(text(f"({cond})").bindparams(p=ctx.path))
        rows = db.execute(q.order_by(Membership.path)).all()
        return [
            Person(m.id, m.user_id, name, email, m.role, m.title, m.team_name, m.path, m.parent_id,
                   m.path.count("."), m.status, m.joined_at)
            for m, name, email in rows
        ]


def get_person(ctx: TenantContext, user_id: uuid.UUID) -> Person:
    for p in list_people(ctx, include_upline=True):
        if p.user_id == user_id:
            return p
    raise NotFound("Person not found")


def my_leader(ctx: TenantContext) -> Person | None:
    with tenant_session(ctx) as db:
        m = db.get(Membership, ctx.membership_id)
        if not m.parent_id:
            return None
        p = db.get(Membership, m.parent_id)
        u = db.get(User, p.user_id)
        return Person(p.id, p.user_id, u.name, u.email, p.role, p.title, p.team_name, p.path, p.parent_id,
                      p.path.count("."), p.status, p.joined_at)


def upline_chain(db, org_id, path: str) -> list[Membership]:
    """Ancestors of a path, nearest first (excluding the node itself)."""
    rows = db.scalars(
        select(Membership).where(Membership.org_id == org_id, Membership.status == "active",
                                 text("memberships.path @> cast(:p as ltree) and memberships.path <> cast(:p as ltree)")
                                 .bindparams(p=path))
        .order_by(func.nlevel(Membership.path).desc())
    ).all()
    return list(rows)


def _assert_in_branch(ctx: TenantContext, m: Membership, allow_self=False) -> None:
    if ctx.is_admin:
        return
    if m.path == ctx.path and allow_self:
        return
    if not m.path.startswith(ctx.path + "."):
        raise PermissionDenied("That person is not in your team.")


def invite(ctx: TenantContext, email: str, role: str = "member", parent_membership_id: uuid.UUID | None = None) -> str:
    """Invite someone into the hierarchy. Leaders may invite only into their own branch,
    and only as members or leaders. Returns the raw token (also emailed)."""
    from .auth import _clean_email

    email = _clean_email(email)
    if role not in ROLES or role == "owner":
        raise ValidationError("Choose a valid role.")
    if role in ADMIN_ROLES and not ctx.is_admin:
        raise PermissionDenied("Only admins can invite admins.")
    with tenant_session(ctx) as db:
        org = db.get(Organization, ctx.org_id)
        settings = {**DEFAULT_SETTINGS, **(org.settings or {})}
        if ctx.is_assistant:
            raise PermissionDenied("Assistants can't invite people.")
        if role == "assistant":
            if not settings.get("allow_assistants", True):
                raise PermissionDenied("Assistants are turned off for this organization.")
            if not ctx.is_admin:
                parent_membership_id = ctx.membership_id  # an assistant works for the agent who invites them
        elif ctx.role == "member":
            if not settings.get("members_can_recruit"):
                raise PermissionDenied("Ask your leader to invite new people.")
            if role != "member":
                raise PermissionDenied("You can invite new team members only.")
        parent_id = parent_membership_id or ctx.membership_id
        parent = db.get(Membership, parent_id)
        if not parent or parent.org_id != ctx.org_id:
            raise NotFound("Team not found")
        _assert_in_branch(ctx, parent, allow_self=True)
        existing = db.scalar(select(Membership.id).join(User, User.id == Membership.user_id)
                             .where(Membership.org_id == ctx.org_id, User.email == email))
        if existing:
            raise ValidationError("That person is already in this organization.")
        raw = new_token()
        days = int((org.settings or {}).get("invite_expiry_days", 7))
        inv = Invitation(org_id=ctx.org_id, email=email, role=role, parent_membership_id=parent.id,
                         token_hash=token_hash(raw), expires_at=_now() + timedelta(days=days), created_by=ctx.user_id)
        db.add(inv)
        db.flush()
        audit.record(db, ctx, "invitation.created", "invitation", inv.id, email=email, role=role)
    link = f"{get_settings().base_url}/invite?token={raw}"
    mailer.send_email(email, f"You're invited to join {org.name}",
                      f"{ctx.user_name} invited you to join {org.name} on {get_settings().app_name}.\n\n"
                      f"Accept the invitation (link expires in {days} days):\n{link}\n", from_name=org.name)
    return raw


def list_invitations(ctx: TenantContext) -> list[Invitation]:
    with tenant_session(ctx) as db:
        return list(db.scalars(select(Invitation).where(
            Invitation.org_id == ctx.org_id, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None),
            Invitation.expires_at > _now()).order_by(Invitation.created_at.desc())))


def revoke_invitation(ctx: TenantContext, invitation_id: uuid.UUID) -> None:
    with tenant_session(ctx) as db:
        inv = db.get(Invitation, invitation_id)
        if not inv:
            raise NotFound("Invitation not found")
        inv.revoked_at = _now()
        audit.record(db, ctx, "invitation.revoked", "invitation", inv.id)


def set_role(ctx: TenantContext, membership_id: uuid.UUID, role: str, title: str | None = None) -> None:
    if role not in ROLES:
        raise ValidationError("Unknown role.")
    with tenant_session(ctx) as db:
        m = db.get(Membership, membership_id)
        if not m:
            raise NotFound("Person not found")
        if m.id == ctx.membership_id:
            raise PermissionDenied("You can't change your own role.")
        if role in ADMIN_ROLES or m.role in ADMIN_ROLES:
            ctx.require("owner") if "owner" in (role, m.role) else ctx.require_admin()
        else:
            ctx.require("owner", "admin", "leader")
            _assert_in_branch(ctx, m)
        old = m.role
        m.role = role
        if title is not None:
            m.title = title.strip()[:120] or None
        audit.record(db, ctx, "membership.role_changed", "membership", m.id, old=old, new=role)


def move_member(ctx: TenantContext, membership_id: uuid.UUID, new_parent_id: uuid.UUID | None) -> None:
    """Re-parent a person together with their whole downline."""
    with tenant_session(ctx) as db:
        m = db.get(Membership, membership_id)
        if not m:
            raise NotFound("Person not found")
        new_parent = db.get(Membership, new_parent_id) if new_parent_id else None
        if new_parent_id and not new_parent:
            raise NotFound("New leader not found")
        if not ctx.is_admin:
            _assert_in_branch(ctx, m)
            if not new_parent:
                raise PermissionDenied("Only admins can move someone to the top level.")
            _assert_in_branch(ctx, new_parent, allow_self=True)
        if new_parent and (new_parent.path == m.path or new_parent.path.startswith(m.path + ".")):
            raise ValidationError("Can't move someone under their own downline.")
        new_path = f"{new_parent.path}.{_label(m.id)}" if new_parent else _label(m.id)
        db.execute(text(
            "update memberships set path = cast(:new as ltree) || subpath(path, nlevel(cast(:old as ltree))) "
            "where org_id = :org and path <@ cast(:old as ltree) and path <> cast(:old as ltree)"
        ), {"new": new_path, "old": m.path, "org": ctx.org_id})
        old_path = m.path
        m.path = new_path
        m.parent_id = new_parent.id if new_parent else None
        audit.record(db, ctx, "membership.moved", "membership", m.id, old_path=old_path, new_path=new_path)


def release_member(ctx: TenantContext, membership_id: uuid.UUID, note: str = "") -> None:
    """Someone leaves the organization: their downline moves up to their leader,
    their access ends, and the action is audited."""
    ctx.require_admin()
    with tenant_session(ctx) as db:
        m = db.get(Membership, membership_id)
        if not m:
            raise NotFound("Person not found")
        if m.role == "owner":
            raise PermissionDenied("Transfer ownership before releasing the owner.")
        children = db.scalars(select(Membership.id).where(Membership.parent_id == m.id)).all()
    for child_id in children:
        move_member(ctx, child_id, m.parent_id)
    with tenant_session(ctx) as db:
        m = db.get(Membership, membership_id)
        m.status = "left"
        audit.record(db, ctx, "membership.released", "membership", m.id, note=note, downline_moved=len(children))
        released_user = m.user_id
    from .prospects import mark_owner_released
    from .tasks import release_user_tasks

    mark_owner_released(ctx.org_id, released_user)
    release_user_tasks(ctx.org_id, released_user)


def list_activity_types(ctx: TenantContext, include_inactive=False) -> list[ActivityType]:
    with tenant_session(ctx) as db:
        q = select(ActivityType).where(ActivityType.org_id == ctx.org_id)
        if not include_inactive:
            q = q.where(ActivityType.active.is_(True))
        return list(db.scalars(q.order_by(ActivityType.position)))


def save_activity_type(ctx: TenantContext, label: str, daily_target: int, type_id: uuid.UUID | None = None,
                       active: bool = True) -> None:
    ctx.require_admin()
    label = (label or "").strip()
    if not label:
        raise ValidationError("Enter a label.")
    with tenant_session(ctx) as db:
        if type_id:
            t = db.get(ActivityType, type_id)
            if not t:
                raise NotFound("Activity type not found")
        else:
            n = db.scalar(select(func.count()).select_from(ActivityType).where(ActivityType.org_id == ctx.org_id))
            t = ActivityType(org_id=ctx.org_id, key=slugify(label).replace("-", "_")[:40] + f"_{n}", position=n)
            db.add(t)
        t.label = label[:80]
        t.daily_target = max(0, int(daily_target or 0))
        t.active = active
        audit.record(db, ctx, "activity_type.saved", "activity_type", t.id, label=label)


def audit_entries(ctx: TenantContext, limit: int = 200):
    from ..models import AuditLog

    ctx.require_admin()
    with tenant_session(ctx) as db:
        rows = db.execute(select(AuditLog, User.name).outerjoin(User, User.id == AuditLog.actor_id)
                          .where(AuditLog.org_id == ctx.org_id).order_by(AuditLog.id.desc()).limit(limit)).all()
        return [(a, n) for a, n in rows]
