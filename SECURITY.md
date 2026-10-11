# Security design

This describes the controls built into TeamPath and what remains the operator's responsibility.

## Isolation

| Layer | Control |
|---|---|
| Database role | The app connects as `teampath_app`, which does not own any table, so row-level security (RLS) always applies. Migrations use a separate owner role. |
| Tenant | Every tenant-owned table has `org_id` and an RLS policy: `org_id = current organization`. Inserting into another organization fails. |
| Hierarchy | Person-scoped data (enrollments, progress, goals, activity, questions, updates, momentum) is visible to the person, their upline (ancestors in the `ltree` path) and org admins — never to siblings or to the downline. |
| Private | Personal map layouts, focus pins and notes about people are visible only to their author — admins included. |
| Prospects | `prospects`, `prospect_activities`, `prospect_lists`, `prospect_list_items` are owner-only (`owner_id = current user`) — no upline or admin access. Phone, email, notes and activity notes are Fernet-encrypted; HMAC-SHA256 hashes (keyed with `SECRET_KEY`) find duplicates and exact phone/email matches. |
| Activity privacy | Organizations can make daily activity visible only through weekly updates (enforced in RLS). |
| Content scope | Leaders' content and paths are scoped to their branch (`scope_path`) by RLS. |
| Global tables | `users` are visible only to people who share an organization; `auth_tokens` only to their owner; `organizations` only to members. |
| Bypass | Cross-tenant access (`app.bypass`) exists only in `db.system_session(reason)`, used for authentication lookups, platform-admin screens and background jobs. Each call site states its reason. |
| Tests | `tests/test_isolation.py` checks every tenant table for cross-tenant leaks and sibling-branch leaks, and fails CI if a table with `org_id` lacks RLS. |

RLS protects against bugs (a forgotten filter). It does not protect against an attacker who can run arbitrary code inside the app container. Keep the container patched and minimal.

## Authentication & sessions

- Argon2id password hashing; minimum length 12; common-password list; optional Have I Been Pwned k-anonymity check (`HIBP_CHECK=true`).
- Account lockout after 5 failed attempts for 15 minutes (configurable). Login responses don't reveal whether an account exists.
- TOTP two-step verification. Each organization chooses which roles must use it (default: owners, admins, leaders). Secrets are encrypted at rest with `ENCRYPTION_KEY` (Fernet).
- Session tokens are random, stored only as SHA-256 hashes, revocable per device, and invalidated on password change or reset. "Keep me signed in" stores the token in the browser; otherwise it lives only in server memory.
- Idle timeout (30 min) and absolute lifetime (12 h, or 14 days when remembered).
- Invitation, verification and reset links are single-use, expiring and hashed in the database.
- Production refuses to start with default secrets or without an encryption key.

## Authorization

- All business rules live in `teampath/services/`. Every function receives a `TenantContext` and checks role and hierarchy before acting. UI visibility is a convenience, not a control.
- Leaders act only within their branch. Admins can't change their own role, and only owners can grant or remove owner/admin roles. Releasing a member is admin-only and audited.

## Files

- Uploads use Flet's signed, expiring upload URLs, and are written only under `/data/incoming/<random session prefix>`.
- Server-side checks: extension allow-list, size limit, and content signature (magic bytes must match the extension).
- Optional ClamAV scan. Files aren't viewable until they're marked clean (or scanning is disabled). Infected files are deleted and audited.
- Logos are decoded and re-encoded as PNG, which strips metadata and anything appended to the image. SVG isn't accepted.
- Document conversion runs LibreOffice in the worker container with a throwaway profile and a 3-minute timeout.
- Downloads and previews use per-user HMAC-signed URLs valid for 5–15 minutes. They're re-checked against the user's current membership on every request.
- Optional per-item view-only mode and per-viewer watermarking of rendered pages.

## Web

- Security headers: CSP (`CSP_MODE=enforce|report|off`), HSTS (production), `X-Frame-Options: DENY`, `nosniff`, a strict referrer policy, and a permissions policy. Flet's wildcard CORS header is removed.
- No OpenAPI/docs endpoints exposed.
- Traefik rate limiting on the router (tune in `docker-compose.yml`).

## Audit

`audit_log` is append-only: the app role has INSERT and SELECT only. It records:

- Sign-ins and failures
- MFA changes
- Session revocations
- Role and hierarchy changes
- Invitations
- Uploads, downloads and content views
- Sign-offs
- Settings and branding changes
- Platform-admin actions

Org admins can view their organization's log in the app.

## Delegation

- `tasks` and `task_comments` are visible only to the task's owner and assignee. A task can be created only by its owner, for themselves or for an **active** assistant (`assistant_links`). The database checks this on insert.
- Delegated prospect access is granted by RLS only while a task about that prospect is open and assigned to the viewer. It covers contact details, stage and recent activity. The agent's private notes (`details_enc`) are never shown to assistants.
- Assistants can record only scheduling outcomes (no answer, voicemail, sent, talked, appointment set, check back later, note). Their entries carry `by_user_id` and are not counted as the agent's activity.
- When an assistant link ends or a member is released, their open delegated tasks return to the owner.

## Notes

- `notes`, `note_photos` and `note_actions` are visible only to the author (admins included). Note text is Fernet-encrypted.
- `note_events`: private to the creator, or a team event visible to the creator's downline (`scope_path`). Only leaders and admins create team events; only admins create organization-wide ones (null scope). The database checks the scope on write.
- `shared_notes` are snapshots, visible to the author, the explicit recipients (`shared_note_recipients`) and, for team sharing, the author's downline — never assistants, siblings or the wider upline. Recipients can be added only by the note's author (a `SECURITY DEFINER` check, so the two tables' policies don't refer to each other). Comments follow the shared note's visibility and are encrypted.
- Slide photos are decoded and re-encoded as JPEG (rotation applied, EXIF/GPS and appended data removed, max 2000 px), then go through the normal upload checks. They're served through per-user signed URLs only after the service confirms the viewer can see the note.
- Deleting a note deletes its photos and shared copy. Sharing and unsharing are audited, without the note's content.

## Prospects lifecycle

- Agent-owned. On release, prospects are flagged and deleted after 30 days; nobody else is given access.
- Retention (organization setting): archive after N months without activity (default 12), delete after M months (default 24).
- Exports are single-use files behind a 5-minute link signed for the requesting user, deleted after download (or within 15 minutes) and audited. Imports are audited (counts only — never contact details).
- No messages are sent by the platform. Do-not-call and no-text flags are enforced both in the UI and in the service.

## Data you should not store here

Policy details, health information, SSNs, dates of birth, bank details and commission figures. Prospect records are for contact details and short follow-up notes only. Keeping them in your CRM and carrier systems keeps this platform outside the scope of regulations such as GLBA and state insurance data-security laws. The opportunity-sharing feature deliberately stores only a short label.

## Operator responsibilities

- Strong, unique values in `.env` (`chmod 600`), SMTP with SPF/DKIM, and TLS via Traefik.
- Firewall, SSH keys only, automatic security updates, `fail2ban`.
- Encrypted, off-server backups, with restores tested.
- Turn on two-step verification for your own platform-admin account.
- Run the ClamAV profile if users upload files from outside your organization.
- Commission an external penetration test before onboarding real agencies, and repeat it after major changes.

## Reporting

Report suspected vulnerabilities privately to the maintainer rather than in a public issue.
