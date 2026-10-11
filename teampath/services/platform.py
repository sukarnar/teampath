"""Platform-administrator operations (cross-tenant; every call re-checks the flag and is audited)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from .. import audit, mailer
from ..context import PermissionDenied, ValidationError
from ..db import system_session
from ..models import Membership, Organization, User


@dataclass
class OrgRow:
    id: uuid.UUID
    name: str
    slug: str
    org_type: str
    status: str
    members: int
    owner_email: str | None
    created_at: datetime


def _require_platform_admin(db, user_id) -> None:
    u = db.get(User, user_id)
    if not u or not u.is_platform_admin or u.status != "active":
        raise PermissionDenied("Platform administrators only.")


def list_orgs(admin_user_id: uuid.UUID, status: str | None = None) -> list[OrgRow]:
    with system_session("platform admin: list organizations") as db:
        _require_platform_admin(db, admin_user_id)
        q = select(Organization).order_by(Organization.created_at.desc())
        if status:
            q = q.where(Organization.status == status)
        orgs = list(db.scalars(q))
        counts = dict(db.execute(select(Membership.org_id, func.count()).where(Membership.status == "active")
                                 .group_by(Membership.org_id)).all())
        owners = dict(db.execute(select(Membership.org_id, User.email).join(User, User.id == Membership.user_id)
                                 .where(Membership.role == "owner")).all())
        return [OrgRow(o.id, o.name, o.slug, o.org_type, o.status, counts.get(o.id, 0), owners.get(o.id), o.created_at)
                for o in orgs]


def set_org_status(admin_user_id: uuid.UUID, org_id: uuid.UUID, status: str, reason: str = "") -> None:
    if status not in ("active", "suspended"):
        raise ValidationError("Unknown status")
    with system_session("platform admin: change organization status") as db:
        _require_platform_admin(db, admin_user_id)
        org = db.get(Organization, org_id)
        old = org.status
        org.status = status
        audit.record(db, None, f"platform.org_{status}", "organization", org.id, org_id=org.id, actor_id=admin_user_id,
                     old=old, reason=reason)
        owner = db.scalar(select(User.email).join(Membership, Membership.user_id == User.id)
                          .where(Membership.org_id == org.id, Membership.role == "owner"))
    if owner and old == "pending" and status == "active":
        mailer.send_email(owner, f"{org.name} is approved", "Your organization is approved. You can sign in now.")
