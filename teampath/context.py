"""Who is acting, in which organization, with which role."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_LEADER = "leader"
ROLE_MEMBER = "member"
ROLE_ASSISTANT = "assistant"  # works for one or more agents; not part of anyone's sales team
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_LEADER, ROLE_MEMBER, ROLE_ASSISTANT)
ADMIN_ROLES = (ROLE_OWNER, ROLE_ADMIN)
AUTHOR_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_LEADER)


class PermissionDenied(Exception):
    """Raised when the acting user may not perform an action."""


class NotFound(Exception):
    pass


class ValidationError(Exception):
    pass


@dataclass(frozen=True)
class TenantContext:
    user_id: uuid.UUID
    org_id: uuid.UUID
    membership_id: uuid.UUID
    role: str
    path: str  # ltree path of the user's membership node
    user_name: str = ""
    user_email: str = ""
    org_type: str = "generic"
    activity_visible_upline: bool = True
    timezone: str = "America/New_York"
    ip: str | None = None
    extra: dict = field(default_factory=dict, compare=False)

    def today(self) -> date:
        """Today's date in the organization's time zone (the server runs on UTC)."""
        try:
            return datetime.now(ZoneInfo(self.timezone)).date()
        except Exception:
            return datetime.now(ZoneInfo("America/New_York")).date()

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES

    @property
    def is_assistant(self) -> bool:
        return self.role == ROLE_ASSISTANT

    @property
    def is_author(self) -> bool:
        return self.role in AUTHOR_ROLES

    def require(self, *roles: str) -> None:
        if self.role not in roles:
            raise PermissionDenied("You don't have permission to do that.")

    def require_admin(self) -> None:
        self.require(*ADMIN_ROLES)

    def require_author(self) -> None:
        self.require(*AUTHOR_ROLES)

    def is_ancestor_of(self, other_path: str) -> bool:
        """True if `other_path` is inside this user's branch (or is the user)."""
        return other_path == self.path or other_path.startswith(self.path + ".")
