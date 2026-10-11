"""Accounts, login, MFA, sessions, invitations."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from .. import audit, mailer
from ..config import get_settings
from ..context import ADMIN_ROLES, PermissionDenied, TenantContext, ValidationError
from ..db import system_session, tenant_session, user_session
from ..models import AuthToken, Invitation, Membership, OrgBranding, Organization, User
from ..security import (
    decrypt,
    encrypt,
    hash_password,
    new_token,
    new_totp_secret,
    password_problems,
    token_hash,
    totp_uri,
    verify_password,
    verify_totp,
)
from . import orgs as orgs_service

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SignedInUser:
    id: uuid.UUID
    email: str
    name: str
    mfa_enabled: bool
    is_platform_admin: bool


@dataclass
class LoginResult:
    status: str  # ok | mfa_required | invalid | locked | unverified | disabled
    user: SignedInUser | None = None
    message: str = ""


def _signed(u: User) -> SignedInUser:
    return SignedInUser(u.id, u.email, u.name, u.mfa_enabled, u.is_platform_admin)


def _clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ValidationError("Enter a valid email address.")
    return email


# ------------------------------------------------------------ sign up

def signup_organization(*, name: str, email: str, password: str, org_name: str, org_type: str = "generic",
                        ip: str | None = None) -> uuid.UUID:
    """Creates a user, an organization (pending approval if configured) and the owner membership."""
    s = get_settings()
    email = _clean_email(email)
    name = (name or "").strip()
    if not name:
        raise ValidationError("Enter your name.")
    if problems := password_problems(password, email, name):
        raise ValidationError(" ".join(problems))
    with system_session("signup: create user and organization") as db:
        if db.scalar(select(User.id).where(User.email == email)):
            # Do not reveal whether an account exists; tell them to sign in instead.
            raise ValidationError("Could not create the account. If you already have one, sign in instead.")
        user = User(email=email, name=name, password_hash=hash_password(password),
                    email_verified=not bool(s.smtp_host) and not s.is_production,
                    password_changed_at=now())
        db.add(user)
        db.flush()
        org = orgs_service.create_org_record(db, name=org_name, org_type=org_type,
                                             status="pending" if s.require_org_approval else "active")
        orgs_service.add_root_membership(db, org.id, user.id, role="owner")
        audit.record(db, None, "org.signup", "organization", org.id, org_id=org.id, actor_id=user.id, ip=ip)
        if not user.email_verified:
            _send_verification(db, user)
        return user.id


def _send_verification(db, user: User) -> None:
    raw = new_token()
    db.add(AuthToken(user_id=user.id, kind="verify", token_hash=token_hash(raw),
                     expires_at=now() + timedelta(days=2)))
    link = f"{get_settings().base_url}/verify-email?token={raw}"
    mailer.send_email(user.email, "Confirm your email",
                      f"Hi {user.name},\n\nConfirm your email to finish setting up your account:\n{link}\n")


def verify_email(raw: str) -> bool:
    with system_session("auth: verify email token") as db:
        t = db.scalar(select(AuthToken).where(AuthToken.token_hash == token_hash(raw), AuthToken.kind == "verify"))
        if not t or t.revoked_at or t.expires_at < now():
            return False
        db.execute(update(User).where(User.id == t.user_id).values(email_verified=True))
        t.revoked_at = now()
        audit.record(db, None, "user.email_verified", "user", t.user_id, actor_id=t.user_id)
        return True


# ------------------------------------------------------------ login

def login(email: str, password: str, ip: str | None = None) -> LoginResult:
    s = get_settings()
    email = (email or "").strip().lower()
    with system_session("auth: password login") as db:
        u = db.scalar(select(User).where(User.email == email))
        if u and u.locked_until and u.locked_until > now():
            audit.record(db, None, "auth.login_locked", "user", u.id, actor_id=u.id, ip=ip)
            return LoginResult("locked", message="Too many attempts. Try again later.")
        if not u or not verify_password(u.password_hash, password):
            if u:
                u.failed_logins += 1
                if u.failed_logins >= s.max_failed_logins:
                    u.locked_until = now() + timedelta(minutes=s.lockout_minutes)
                    u.failed_logins = 0
                audit.record(db, None, "auth.login_failed", "user", u.id, actor_id=u.id, ip=ip)
            return LoginResult("invalid", message="Email or password is incorrect.")
        if u.status != "active":
            return LoginResult("disabled", message="This account is disabled.")
        if not u.email_verified:
            return LoginResult("unverified", message="Confirm your email first. We sent you a link.")
        u.failed_logins = 0
        if u.mfa_enabled:
            return LoginResult("mfa_required", _signed(u))
        u.last_login_at = now()
        audit.record(db, None, "auth.login", "user", u.id, actor_id=u.id, ip=ip)
        return LoginResult("ok", _signed(u))


def verify_login_mfa(user_id: uuid.UUID, code: str, ip: str | None = None) -> LoginResult:
    s = get_settings()
    with system_session("auth: verify MFA code") as db:
        u = db.get(User, user_id)
        if not u or not u.mfa_enabled or not u.mfa_secret_enc:
            return LoginResult("invalid", message="Sign in again.")
        if u.locked_until and u.locked_until > now():
            return LoginResult("locked", message="Too many attempts. Try again later.")
        if not verify_totp(decrypt(u.mfa_secret_enc), code):
            u.failed_logins += 1
            if u.failed_logins >= s.max_failed_logins:
                u.locked_until = now() + timedelta(minutes=s.lockout_minutes)
                u.failed_logins = 0
            audit.record(db, None, "auth.mfa_failed", "user", u.id, actor_id=u.id, ip=ip)
            return LoginResult("invalid", message="That code didn't work.")
        u.failed_logins = 0
        u.last_login_at = now()
        audit.record(db, None, "auth.login", "user", u.id, actor_id=u.id, ip=ip, mfa=True)
        return LoginResult("ok", _signed(u))


def create_session_token(user_id: uuid.UUID, remember: bool, user_agent: str | None, ip: str | None) -> str:
    s = get_settings()
    raw = new_token()
    lifetime = timedelta(days=s.remember_days) if remember else timedelta(hours=s.session_max_hours)
    with user_session(user_id) as db:
        db.add(AuthToken(user_id=user_id, kind="session", token_hash=token_hash(raw), expires_at=now() + lifetime,
                         user_agent=(user_agent or "")[:300], ip=ip, last_seen_at=now()))
    return raw


def resume_session(raw: str) -> SignedInUser | None:
    """Restores a remembered sign-in from the device's stored token."""
    if not raw:
        return None
    with system_session("auth: resume session token") as db:
        t = db.scalar(select(AuthToken).where(AuthToken.token_hash == token_hash(raw), AuthToken.kind == "session"))
        if not t or t.revoked_at or t.expires_at < now():
            return None
        u = db.get(User, t.user_id)
        if not u or u.status != "active":
            return None
        if u.password_changed_at and t.created_at < u.password_changed_at:
            return None
        t.last_seen_at = now()
        return _signed(u)


def session_token_valid(raw: str) -> bool:
    return resume_session(raw) is not None


def revoke_session_token(raw: str) -> None:
    if not raw:
        return
    with system_session("auth: sign out") as db:
        db.execute(update(AuthToken).where(AuthToken.token_hash == token_hash(raw)).values(revoked_at=now()))


def list_sessions(user_id: uuid.UUID) -> list[AuthToken]:
    with user_session(user_id) as db:
        return list(db.scalars(select(AuthToken).where(
            AuthToken.user_id == user_id, AuthToken.kind == "session", AuthToken.revoked_at.is_(None),
            AuthToken.expires_at > now()).order_by(AuthToken.last_seen_at.desc())))


def revoke_session(user_id: uuid.UUID, token_id: uuid.UUID) -> None:
    with user_session(user_id) as db:
        db.execute(update(AuthToken).where(AuthToken.id == token_id, AuthToken.user_id == user_id).values(revoked_at=now()))
        audit.record(db, None, "auth.session_revoked", "auth_token", token_id, actor_id=user_id)


def revoke_all_sessions(user_id: uuid.UUID, except_raw: str | None = None) -> None:
    keep = token_hash(except_raw) if except_raw else ""
    with user_session(user_id) as db:
        db.execute(update(AuthToken).where(AuthToken.user_id == user_id, AuthToken.kind == "session",
                                           AuthToken.token_hash != keep, AuthToken.revoked_at.is_(None))
                   .values(revoked_at=now()))
        audit.record(db, None, "auth.sessions_revoked_all", "user", user_id, actor_id=user_id)


# ------------------------------------------------------------ MFA enrolment

def begin_mfa_setup(user: SignedInUser) -> tuple[str, str]:
    """Returns (secret, otpauth uri). The secret is stored only after confirmation."""
    secret = new_totp_secret()
    return secret, totp_uri(secret, user.email)


def confirm_mfa_setup(user_id: uuid.UUID, secret: str, code: str) -> bool:
    if not verify_totp(secret, code):
        return False
    with user_session(user_id) as db:
        db.execute(update(User).where(User.id == user_id).values(mfa_secret_enc=encrypt(secret), mfa_enabled=True))
        audit.record(db, None, "auth.mfa_enabled", "user", user_id, actor_id=user_id)
    return True


def disable_mfa(user_id: uuid.UUID, password: str, code: str) -> bool:
    with user_session(user_id) as db:
        u = db.get(User, user_id)
        if not verify_password(u.password_hash, password) or not u.mfa_secret_enc \
                or not verify_totp(decrypt(u.mfa_secret_enc), code):
            return False
        u.mfa_enabled = False
        u.mfa_secret_enc = None
        audit.record(db, None, "auth.mfa_disabled", "user", user_id, actor_id=user_id)
    return True


def change_password(user_id: uuid.UUID, current: str, new: str, keep_token: str | None = None) -> None:
    with user_session(user_id) as db:
        u = db.get(User, user_id)
        if not verify_password(u.password_hash, current):
            raise ValidationError("Your current password is incorrect.")
        if problems := password_problems(new, u.email, u.name):
            raise ValidationError(" ".join(problems))
        u.password_hash = hash_password(new)
        u.password_changed_at = now()
        audit.record(db, None, "auth.password_changed", "user", user_id, actor_id=user_id)
    revoke_all_sessions(user_id, except_raw=keep_token)
    if keep_token:
        # The current device keeps working; push its creation time past the change.
        with system_session("auth: keep current session after password change") as db:
            db.execute(update(AuthToken).where(AuthToken.token_hash == token_hash(keep_token))
                       .values(created_at=now() + timedelta(seconds=1)))


def request_password_reset(email: str) -> None:
    """Always behaves the same whether or not the account exists."""
    try:
        email = _clean_email(email)
    except ValidationError:
        return
    with system_session("auth: password reset request") as db:
        u = db.scalar(select(User).where(User.email == email))
        if not u or u.status != "active":
            return
        raw = new_token()
        db.add(AuthToken(user_id=u.id, kind="reset", token_hash=token_hash(raw), expires_at=now() + timedelta(hours=1)))
        link = f"{get_settings().base_url}/reset?token={raw}"
        mailer.send_email(u.email, "Reset your password",
                          f"Use this link within an hour to choose a new password:\n{link}\n\n"
                          "If you didn't ask for this, you can ignore this email.")


def reset_password(raw: str, new_password: str) -> None:
    with system_session("auth: password reset") as db:
        t = db.scalar(select(AuthToken).where(AuthToken.token_hash == token_hash(raw), AuthToken.kind == "reset"))
        if not t or t.revoked_at or t.expires_at < now():
            raise ValidationError("This reset link has expired. Request a new one.")
        u = db.get(User, t.user_id)
        if problems := password_problems(new_password, u.email, u.name):
            raise ValidationError(" ".join(problems))
        u.password_hash = hash_password(new_password)
        u.password_changed_at = now()
        u.failed_logins = 0
        u.locked_until = None
        t.revoked_at = now()
        db.execute(update(AuthToken).where(AuthToken.user_id == u.id, AuthToken.kind == "session")
                   .values(revoked_at=now()))
        audit.record(db, None, "auth.password_reset", "user", u.id, actor_id=u.id)


# ------------------------------------------------------------ organizations for a user

@dataclass
class OrgChoice:
    org_id: uuid.UUID
    org_name: str
    org_status: str
    role: str


def my_organizations(user_id: uuid.UUID) -> list[OrgChoice]:
    with user_session(user_id) as db:
        rows = db.execute(
            select(Membership.org_id, Organization.name, Organization.status, Membership.role)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user_id, Membership.status == "active")
            .order_by(Organization.name)
        ).all()
        return [OrgChoice(*r) for r in rows]


def open_organization(user: SignedInUser, org_id: uuid.UUID, ip: str | None = None) -> TenantContext:
    """Builds the TenantContext used for every later call in this session."""
    with user_session(user.id) as db:
        m = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.org_id == org_id,
                                               Membership.status == "active"))
        org = db.get(Organization, org_id) if m else None
        if not m or not org:
            raise PermissionDenied("You are not a member of that organization.")
        if org.status == "suspended":
            raise PermissionDenied("This organization is suspended.")
        if org.status == "pending":
            raise PermissionDenied("This organization is waiting for approval.")
        settings = org.settings or {}
        return TenantContext(
            user_id=user.id, org_id=org.id, membership_id=m.id, role=m.role, path=m.path,
            user_name=user.name, user_email=user.email, org_type=org.org_type,
            activity_visible_upline=settings.get("activity_visibility", "auto") == "auto",
            timezone=org.timezone or "America/New_York",
            ip=ip,
        )


def mfa_required_for(ctx: TenantContext) -> bool:
    with tenant_session(ctx) as db:
        org = db.get(Organization, ctx.org_id)
        roles = (org.settings or {}).get("mfa_required_roles", ["owner", "admin", "leader"])
        return ctx.role in roles


# ------------------------------------------------------------ invitations

@dataclass
class InvitationInfo:
    org_name: str
    email: str
    role: str
    has_account: bool
    primary_color: str


def inspect_invitation(raw: str) -> InvitationInfo | None:
    with system_session("auth: inspect invitation token") as db:
        inv = db.scalar(select(Invitation).where(Invitation.token_hash == token_hash(raw)))
        if not inv or inv.accepted_at or inv.revoked_at or inv.expires_at < now():
            return None
        org = db.get(Organization, inv.org_id)
        if org.status != "active":
            return None
        brand = db.get(OrgBranding, org.id)
        has_account = db.scalar(select(User.id).where(User.email == inv.email)) is not None
        return InvitationInfo(org.name, inv.email, inv.role, has_account, brand.primary_color if brand else "#3355CC")


def accept_invitation(raw: str, *, name: str = "", password: str = "", existing_password: str = "",
                      ip: str | None = None) -> uuid.UUID:
    with system_session("auth: accept invitation") as db:
        inv = db.scalar(select(Invitation).where(Invitation.token_hash == token_hash(raw)).with_for_update())
        if not inv or inv.accepted_at or inv.revoked_at or inv.expires_at < now():
            raise ValidationError("This invitation is no longer valid.")
        u = db.scalar(select(User).where(User.email == inv.email))
        if u:
            if not verify_password(u.password_hash, existing_password):
                raise ValidationError("Enter the password for your existing account.")
        else:
            if not name.strip():
                raise ValidationError("Enter your name.")
            if problems := password_problems(password, inv.email, name):
                raise ValidationError(" ".join(problems))
            # The invite link proves control of the email address.
            u = User(email=inv.email, name=name.strip(), password_hash=hash_password(password),
                     email_verified=True, password_changed_at=now())
            db.add(u)
            db.flush()
        if db.scalar(select(Membership.id).where(Membership.org_id == inv.org_id, Membership.user_id == u.id)):
            raise ValidationError("You're already a member of this organization.")
        parent = db.get(Membership, inv.parent_membership_id) if inv.parent_membership_id else None
        orgs_service.add_membership(db, inv.org_id, u.id, role=inv.role, parent=parent)
        inv.accepted_at = now()
        audit.record(db, None, "invitation.accepted", "invitation", inv.id, org_id=inv.org_id, actor_id=u.id, ip=ip)
        return u.id


def is_admin_role(role: str) -> bool:
    return role in ADMIN_ROLES
