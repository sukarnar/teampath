"""SQLAlchemy models.

Every tenant-owned table carries `org_id`; row-level security policies are
created in the initial migration (migrations/versions/0001_initial.py).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .db import Ltree


def _uuid():
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _org():
    return mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True)


def _user(nullable=False, index=True):
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=nullable, index=index
    )


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- identity

class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid()
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | disabled
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class AuthToken(Base):
    """Hashed, revocable tokens: login sessions, email verification, password reset."""

    __tablename__ = "auth_tokens"
    id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = _user()
    kind: Mapped[str] = mapped_column(String(20))  # session | verify | reset
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- tenancy

class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _uuid()
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    org_type: Mapped[str] = mapped_column(String(30), default="generic")  # generic | insurance
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | active | suspended
    timezone: Mapped[str] = mapped_column(String(60), default="America/New_York")
    website: Mapped[str | None] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


class OrgBranding(Base):
    __tablename__ = "org_branding"
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    logo_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    primary_color: Mapped[str] = mapped_column(String(9), default="#3355CC")
    tagline: Mapped[str | None] = mapped_column(String(200))
    welcome_text: Mapped[str | None] = mapped_column(Text)


class Membership(Base):
    """A user's place in one organization's hierarchy (upline / downline)."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id"),
        Index("ix_memberships_path", "path", postgresql_using="gist"),
    )
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _user()
    role: Mapped[str] = mapped_column(String(20), default="member")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    path: Mapped[str] = mapped_column(Ltree)
    title: Mapped[str | None] = mapped_column(String(120))  # e.g. "Senior Agent"
    team_name: Mapped[str | None] = mapped_column(String(120))  # leader's own team brand
    team_logo_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | suspended | left
    joined_at: Mapped[datetime] = _created()


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    email: Mapped[str] = mapped_column(CITEXT)
    role: Mapped[str] = mapped_column(String(20), default="member")
    parent_membership_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = _user(index=False)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- files & content

class StoredFile(Base):
    __tablename__ = "files"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    owner_id: Mapped[uuid.UUID] = _user(index=False)
    purpose: Mapped[str] = mapped_column(String(20))  # content | logo | preview
    original_name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(String(500))
    scan_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | clean | infected | skipped
    created_at: Mapped[datetime] = _created()


class ContentItem(Base):
    __tablename__ = "content_items"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))  # document | slides | video | link | text
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    # null = whole organization; otherwise only this branch of the hierarchy
    scope_path: Mapped[str | None] = mapped_column(Ltree)
    allow_download: Mapped[bool] = mapped_column(Boolean, default=True)
    watermark: Mapped[bool] = mapped_column(Boolean, default=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID] = _user(index=False)
    created_at: Mapped[datetime] = _created()


class ContentVersion(Base):
    __tablename__ = "content_versions"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content_items.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL"))
    url: Mapped[str | None] = mapped_column(String(1000))
    body_md: Mapped[str | None] = mapped_column(Text)
    preview_status: Mapped[str] = mapped_column(String(20), default="none")  # none | pending | ready | failed
    preview_pages: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[uuid.UUID] = _user(index=False)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- onboarding paths

class LearningPath(Base):
    __tablename__ = "paths"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    scope_path: Mapped[str | None] = mapped_column(Ltree)
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    draft_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID] = _user(index=False)
    created_at: Mapped[datetime] = _created()


class PathVersion(Base):
    __tablename__ = "path_versions"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    path_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("paths.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published | retired
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class PathStep(Base):
    __tablename__ = "path_steps"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("path_versions.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(80), default="Week 1")
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200))
    instructions_md: Mapped[str | None] = mapped_column(Text)
    step_type: Mapped[str] = mapped_column(String(20), default="task")  # read | task | acknowledge | meet
    content_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("content_items.id", ondelete="SET NULL"))
    due_offset_days: Mapped[int] = mapped_column(Integer, default=7)
    requires_signoff: Mapped[bool] = mapped_column(Boolean, default=False)
    # Stable key so a step can be matched across versions
    step_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), server_default=text("gen_random_uuid()"))


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("user_id", "path_version_id"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _user()
    path_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("paths.id", ondelete="CASCADE"))
    path_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("path_versions.id", ondelete="CASCADE"))
    start_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | completed | cancelled
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class StepProgress(Base):
    __tablename__ = "step_progress"
    __table_args__ = (UniqueConstraint("enrollment_id", "step_id"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    enrollment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("path_steps.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = _user()
    status: Mapped[str] = mapped_column(String(20), default="todo")  # todo | pending_signoff | done
    note: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signed_off_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    signed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Goal(Base):
    __tablename__ = "goals"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _user()
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)
    horizon: Mapped[str] = mapped_column(String(10), default="30")  # 30 | 60 | 90 | custom
    target_date: Mapped[date | None] = mapped_column(Date)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0..100
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed | active | done | dropped
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- Q&A

class Thread(Base):
    __tablename__ = "threads"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    author_id: Mapped[uuid.UUID] = _user()
    target_type: Mapped[str] = mapped_column(String(20), default="general")  # step | content | general
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(200))
    is_question: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | answered | resolved
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_at: Mapped[datetime] = _created()
    created_at: Mapped[datetime] = _created()


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    thread_author_id: Mapped[uuid.UUID] = _user(index=False)  # copied for RLS
    author_id: Mapped[uuid.UUID] = _user(index=False)
    body: Mapped[str] = mapped_column(Text)
    is_answer: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()


class FaqEntry(Base):
    __tablename__ = "faq_entries"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = _user(index=False)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- activity & progress

class ActivityType(Base):
    __tablename__ = "activity_types"
    __table_args__ = (UniqueConstraint("org_id", "key"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    key: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(80))
    daily_target: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    __table_args__ = (Index("ix_activity_user_day", "org_id", "user_id", "logged_on"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _user(index=False)
    type_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("activity_types.id", ondelete="CASCADE"))
    count: Mapped[int] = mapped_column(Integer, default=1)
    logged_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created()


class Momentum(Base):
    __tablename__ = "momentum"
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    week_total: Mapped[int] = mapped_column(Integer, default=0)
    baseline_weekly: Mapped[float] = mapped_column(default=0.0)
    last_activity_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="inactive")  # active | slowing | inactive | new
    computed_at: Mapped[datetime] = _created()


class ProgressUpdate(Base):
    __tablename__ = "progress_updates"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _user()
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    wins: Mapped[str | None] = mapped_column(Text)
    blockers: Mapped[str | None] = mapped_column(Text)
    help_wanted: Mapped[str | None] = mapped_column(Text)
    recipients: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    feedback_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_owner_unseen", "update_owner_id", postgresql_where=text("seen_at is null")),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    update_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("progress_updates.id", ondelete="CASCADE"), index=True)
    update_owner_id: Mapped[uuid.UUID] = _user(index=False)  # copied for RLS
    author_id: Mapped[uuid.UUID] = _user(index=False)
    kind: Mapped[str] = mapped_column(String(20), default="comment")  # comment | coaching | kudos
    body: Mapped[str] = mapped_column(Text)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # when the update's owner saw it
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- team map (private to author)

class FocusPin(Base):
    __tablename__ = "focus_pins"
    __table_args__ = (UniqueConstraint("owner_id", "org_id", "target_user_id"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    owner_id: Mapped[uuid.UUID] = _user()
    target_user_id: Mapped[uuid.UUID] = _user(index=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


class MapLayout(Base):
    __tablename__ = "map_layouts"
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    layout: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"positions": {...}, "groups": {...}}


class PersonNote(Base):
    __tablename__ = "person_notes"
    __table_args__ = (UniqueConstraint("org_id", "author_id", "subject_user_id"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    author_id: Mapped[uuid.UUID] = _user()
    subject_user_id: Mapped[uuid.UUID] = _user(index=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _created()


class OpportunityShare(Base):
    """Record that an agent shared an opportunity with a teammate. No client PII."""

    __tablename__ = "opportunity_shares"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    from_user_id: Mapped[uuid.UUID] = _user()
    to_user_id: Mapped[uuid.UUID] = _user()
    title: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="shared")  # shared | accepted | won | lost
    outcome_note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- platform

class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = {"implicit_returning": False}  # senders may not read recipients' rows
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _user()
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(String(200))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    """Append-only. The app database role has INSERT and SELECT only."""

    __tablename__ = "audit_log"
    __table_args__ = {"implicit_returning": False}  # most roles may write but not read
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(60))
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class Job(Base):
    """Background job queue (Postgres-backed; worker uses SKIP LOCKED)."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = _created()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()




# ---------------------------------------------------------------- prospects (private to their owner)

class Prospect(Base):
    """A sales or recruiting prospect. Owned by one agent; nobody else can read it
    (enforced by row-level security). Contact details and notes are encrypted."""

    __tablename__ = "prospects"
    __table_args__ = (
        Index("ix_prospects_owner_due", "org_id", "owner_id", "next_action_on"),
        Index("ix_prospects_owner_phone", "owner_id", "phone_hash"),
        Index("ix_prospects_owner_email", "owner_id", "email_hash"),
    )
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    owner_id: Mapped[uuid.UUID] = _user(index=False)
    kind: Mapped[str] = mapped_column(String(10), default="sales")  # sales | recruit
    recruit_interest: Mapped[bool] = mapped_column(Boolean, default=False)  # a sales prospect who may also join
    name: Mapped[str] = mapped_column(String(160))
    phone_enc: Mapped[str | None] = mapped_column(Text)
    email_enc: Mapped[str | None] = mapped_column(Text)
    phone_hash: Mapped[str | None] = mapped_column(String(64))
    email_hash: Mapped[str | None] = mapped_column(String(64))
    details_enc: Mapped[str | None] = mapped_column(Text)  # free-form notes about the person
    preferred_contact: Mapped[str | None] = mapped_column(String(10))  # call | text | email
    best_time: Mapped[str | None] = mapped_column(String(60))
    source: Mapped[str | None] = mapped_column(String(20))  # referral | event | social | cold | client | other
    referred_by: Mapped[str | None] = mapped_column(String(120))
    stage: Mapped[str] = mapped_column(String(20), default="new")
    temperature: Mapped[str] = mapped_column(String(10), default="warm")  # hot | warm | cold
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    next_action: Mapped[str | None] = mapped_column(String(160))
    next_action_on: Mapped[date | None] = mapped_column(Date)
    next_action_time: Mapped[str | None] = mapped_column(String(5))  # "14:30"
    dnc: Mapped[bool] = mapped_column(Boolean, default=False)  # do not call
    no_text: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_note: Mapped[str | None] = mapped_column(String(200))
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # owner released from org
    created_at: Mapped[datetime] = _created()


class ProspectActivity(Base):
    __tablename__ = "prospect_activities"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    owner_id: Mapped[uuid.UUID] = _user(index=False)
    prospect_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # call | text | email | meeting | note
    outcome: Mapped[str | None] = mapped_column(String(20))
    note_enc: Mapped[str | None] = mapped_column(Text)
    stage_from: Mapped[str | None] = mapped_column(String(20))
    stage_to: Mapped[str | None] = mapped_column(String(20))
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # an assistant logged it (null = owner)
    created_at: Mapped[datetime] = _created()


class ProspectList(Base):
    __tablename__ = "prospect_lists"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    owner_id: Mapped[uuid.UUID] = _user()
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10))  # smart (saved filter) | focus (snapshot to work through)
    filter: Mapped[dict] = mapped_column(JSONB, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class ProspectListItem(Base):
    __tablename__ = "prospect_list_items"
    __table_args__ = (UniqueConstraint("list_id", "prospect_id"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    owner_id: Mapped[uuid.UUID] = _user(index=False)
    list_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("prospect_lists.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("prospects.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(20))  # outcome logged during the focus session, or "skipped"




# ---------------------------------------------------------------- delegation (agent ↔ assistant)

class AssistantLink(Base):
    """`assistant_id` works for `agent_id` and can receive delegated tasks from them."""

    __tablename__ = "assistant_links"
    __table_args__ = (UniqueConstraint("org_id", "agent_id", "assistant_id"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    agent_id: Mapped[uuid.UUID] = _user()
    assistant_id: Mapped[uuid.UUID] = _user()
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class Task(Base):
    """A to-do owned by an agent, done by the agent or delegated to one of their assistants.
    Visible only to its owner and assignee. Details and notes are encrypted."""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_assignee_open", "org_id", "assignee_id", "status", "due_on"),
                      Index("ix_tasks_owner_open", "org_id", "owner_id", "status"))
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    owner_id: Mapped[uuid.UUID] = _user(index=False)
    assignee_id: Mapped[uuid.UUID] = _user(index=False)
    title: Mapped[str] = mapped_column(String(200))
    details_enc: Mapped[str | None] = mapped_column(Text)
    due_on: Mapped[date | None] = mapped_column(Date)
    due_time: Mapped[str | None] = mapped_column(String(5))
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # normal | high
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | in_progress | done | cancelled
    prospect_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospects.id", ondelete="SET NULL"), index=True)
    completion_note_enc: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _created()
    created_at: Mapped[datetime] = _created()


class TaskComment(Base):
    __tablename__ = "task_comments"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[uuid.UUID] = _user(index=False)
    body_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- notes (events, sessions, sharing)

class NoteEvent(Base):
    """A conference, training or webinar. Private to its creator, or a team event that a
    leader shares with their downline (admins: the whole organization when scope_path is null)."""

    __tablename__ = "note_events"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    owner_id: Mapped[uuid.UUID] = _user()
    title: Mapped[str] = mapped_column(String(200))
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(200))
    organizer: Mapped[str | None] = mapped_column(String(200))
    team_event: Mapped[bool] = mapped_column(Boolean, default=False)
    scope_path: Mapped[str | None] = mapped_column(Ltree)
    created_at: Mapped[datetime] = _created()


class NoteSession(Base):
    """A talk within an event."""

    __tablename__ = "note_sessions"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("note_events.id", ondelete="CASCADE"),
                                                index=True)
    title: Mapped[str] = mapped_column(String(200))
    speaker: Mapped[str | None] = mapped_column(String(200))
    starts_at: Mapped[str | None] = mapped_column(String(5))
    created_by: Mapped[uuid.UUID] = _user(index=False)
    created_at: Mapped[datetime] = _created()


class Note(Base):
    """An agent's own notes. Private to the author; the body is encrypted at rest."""

    __tablename__ = "notes"
    __table_args__ = (Index("ix_notes_author", "org_id", "author_id", "updated_at"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    author_id: Mapped[uuid.UUID] = _user(index=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("note_events.id", ondelete="SET NULL"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("note_sessions.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body_enc: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    updated_at: Mapped[datetime] = _created()
    created_at: Mapped[datetime] = _created()


class NotePhoto(Base):
    __tablename__ = "note_photos"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[uuid.UUID] = _user(index=False)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"))
    caption: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


class NoteAction(Base):
    """An action item from a note that the author turned into a goal or a task."""

    __tablename__ = "note_actions"
    __table_args__ = (UniqueConstraint("note_id", "line_key"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[uuid.UUID] = _user(index=False)
    line_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(10))  # goal | task
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()


class SharedNote(Base):
    """A snapshot of a note that its author shared. Visible to the author, the people it was
    shared with (`shared_note_recipients`) and, when `to_team`, the author's downline."""

    __tablename__ = "shared_notes"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"),
                                               unique=True)
    author_id: Mapped[uuid.UUID] = _user()
    author_path: Mapped[str] = mapped_column(Ltree)
    to_team: Mapped[bool] = mapped_column(Boolean, default=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("note_events.id", ondelete="SET NULL"), index=True)
    event_title: Mapped[str | None] = mapped_column(String(200))
    session_title: Mapped[str | None] = mapped_column(String(200))
    speaker: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    body_enc: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    photo_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    shared_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class SharedNoteRecipient(Base):
    __tablename__ = "shared_note_recipients"
    __table_args__ = (UniqueConstraint("shared_id", "user_id"),)
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    shared_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("shared_notes.id", ondelete="CASCADE"),
                                                 index=True)
    author_id: Mapped[uuid.UUID] = _user(index=False)
    user_id: Mapped[uuid.UUID] = _user()
    is_leader: Mapped[bool] = mapped_column(Boolean, default=False)


class NoteComment(Base):
    __tablename__ = "note_comments"
    id: Mapped[uuid.UUID] = _uuid()
    org_id: Mapped[uuid.UUID] = _org()
    shared_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("shared_notes.id", ondelete="CASCADE"),
                                                 index=True)
    author_id: Mapped[uuid.UUID] = _user(index=False)
    body_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


TENANT_TABLES = [
    t.__tablename__
    for t in Base.__subclasses__()
    if "org_id" in t.__table__.columns and t.__tablename__ not in ("audit_log",)
]
