# TeamPath

A multi-tenant onboarding, training and team-performance platform built with **Python Flet**, **FastAPI** and **PostgreSQL**, packaged for Docker on a VPS behind Traefik.

It is generic at its core, with an **insurance agency** mode for organizations where every agent runs their own business and builds their own team (upline / downline).

> "TeamPath" is a working name. Change `APP_NAME` in `.env` to rebrand the platform; each organization also has its own logo, colors and details.

## What's in the box

| Area | Features |
|---|---|
| **Tenancy** | Organizations with their own logo, brand color, tagline, welcome message and details. Users can belong to several organizations with a different role in each, and switch between them. New organizations can require platform-admin approval. |
| **Hierarchy** | Upline/downline tree (Postgres `ltree`). Agents recruit into their own downline and become leaders automatically when their first recruit joins. Admins can move a person with their whole team, or release someone (their team moves up to their leader). Leaders can name and brand their own team. |
| **Onboarding paths** | Stages and steps (task, read, acknowledge, meet), linked material, due dates relative to the start date, sequential unlocking, leader sign-off. Versioned: editing a path never disturbs people already enrolled; leaders can move them to the latest version and completed steps carry over. |
| **Content library** | Documents (PDF, Word, Excel, PowerPoint), images, video, links and Markdown text. PDFs are viewable the moment they're uploaded (pages render on demand and load in batches, plus an "Open PDF" button); Word/Excel/PowerPoint files are converted by the worker in the background and the page updates itself when ready. Per-item options: team-only, view-only (no download), and viewer-name watermarking. Versions. |
| **Goals** | 30/60/90-day goals proposed by the agent and approved by their leader (or set by the leader), with progress tracking. Leaders get a "To approve" queue (Goals page, Today, Team progress, team map) and can approve or send a goal back with a note. |
| **Q&A** | Questions on any step, material or general topic go to the asker's leader; unanswered questions escalate up the hierarchy and then to admins. Good answers become FAQs shown to everyone. |
| **Daily activity** | One-tap +1 logging of activities the organization defines (insurance defaults: dials, contacts, appointments, applications, recruiting, training), with daily goals. |
| **Team map** | The leader's downline as nested bubbles: color = momentum (active / slowing / inactive / new, compared with each person's *own* baseline), ring = onboarding progress, badges for things needing attention. Tap a person to pin them to *Today's focus*, keep private notes and tags, zoom into their team, or share an opportunity. "My layout" lets each leader drag people into their own private arrangement. List view for accessibility. |
| **Weekly updates** | An auto-filled scorecard (activity vs targets, goals, onboarding) plus wins / blockers / help wanted, sent to the leader (and optionally further up). Leaders reply with comments, coaching notes or kudos. |
| **Prospects (CRM)** | A private, follow-up-driven prospect list for every agent, for both sales and recruiting prospects. Every open prospect always has a next step: logging a call, text, email, meeting or note means picking what happened ("No answer", "Appointment set", "Became a client"…) and when to follow up. Outcomes move the prospect along its pipeline (sales: New → Contacted → Appointment → Presented → Application → Client; recruiting: New → Contacted → Interview → Interviewed → Licensing → Joined) and **count toward daily activity automatically** (dials, contacts, appointments, applications, recruiting conversations). **Lists:** ready-made smart lists (due now, overdue, hot with no appointment, no contact in 14+ days, new referrals, recruits in licensing…), your own saved filters, and **focus lists** you build on demand. A **focus session** works through a list one person at a time: tap-to-call/text on your own phone, log the outcome, set the next step, next. Today shows follow-ups due with a one-tap start. CSV import (with duplicate detection) and export. |
| **Tasks & delegation** | Every agent has a task list, and can **invite assistants** (or make someone in their team their assistant) and **delegate tasks** to them with instructions, due date/time and priority. Tasks are private to the agent and the assistant: leaders and admins don't see them. A task can be about one of the agent's prospects: while it's open, the assistant sees that prospect's contact details (never the agent's private notes) and can log calls and set the next follow-up; access ends automatically when the task is done or cancelled. Assistants can't close a prospect or record presentations or applications, and their calls don't count as the agent's activity. Comments go back and forth with notifications; "Mark done" takes a note. Today shows due and overdue tasks and how delegated work is going. Removing or releasing an assistant hands their open tasks back. Assistants get a simple workspace (Today, Tasks, Learn, Notes, Library, Questions) and don't appear on team maps. |
| **Notes** | Take notes at trainings and conferences, organized by **event** and **session** (talk, speaker, time). Leaders create **team events** their downline picks, so the whole team's notes land in one place; admins can create organization-wide events, and attendees add sessions. One tap marks a line as a **key point, action item, question or stat** (`KEY:` `ACTION:` `Q:` `STAT:`). **Takeaways** collects every marked line for an event, and action items become a **goal** (proposed to the leader) or a **task** (for yourself or your assistant) with one click. Add **slide photos** (re-encoded, so location data is stripped). Topic tags (set by admins, plus your own) and search. Notes save automatically and are **private to the author** — leaders and admins included — with the text encrypted at rest. **Share** a snapshot with your leader, your team and/or specific people; keep editing privately and update the shared copy when ready. Recipients comment, with notifications. The Shared tab groups everyone's notes by event. Notes tagged Financial concepts or Compliance shared beyond your leader show "not compliance-reviewed". |
| **Opportunities** | Record hand-offs between teammates without storing client details. |
| **Notifications** | In-app, live (open pages update when something changes), and email for invitations and account events. |

## Security model

Security is built into each layer, not bolted on. See [SECURITY.md](SECURITY.md) for the full list. The essentials:

- **Row-level security in Postgres.** The app connects as a role that does not own the tables, so every query is filtered by the database itself. A user sees only their organization; within it, only themselves, their downline and their upline; admins see the whole organization. Private notes, layouts and focus pins are visible only to their author, even to admins. Daily activity can be restricted to updates only.
- **Every action is authorized on the server.** Flet runs the UI on the server; every service call re-checks the caller's role and hierarchy position. Hidden buttons are never the security boundary.
- **Accounts:** Argon2id passwords, lockout after failed attempts, optional breached-password check, TOTP two-step verification (required by role, configurable per org), MFA secrets encrypted at rest, idle and absolute session timeouts, revocable device sessions, "sign out everywhere".
- **Files:** content-signature checks (a renamed `.exe` is rejected), optional ClamAV scanning, logos re-encoded, per-user signed URLs that expire in minutes, watermarking and view-only options.
- **Append-only audit log** of logins, role and hierarchy changes, sign-offs, downloads, content views and admin actions; visible to org admins.
- **Notes are private to their author** until shared, and the text is encrypted. Sharing is a snapshot to chosen people (leader, team, individuals in your branch); the database enforces who can read it.
- **Prospects are private to the agent who owns them**: enforced by row-level security, so leaders and admins can't read them either. Phone numbers, emails and notes are encrypted at rest; duplicates are detected with a keyed hash, so nothing has to be decrypted to search. Prospects are **agent-owned**: when someone is released, nobody else receives their prospects and they are deleted after 30 days (agents can export first). Inactive prospects are archived and later deleted on an org-configurable schedule. The platform **never sends texts or emails**; tap-to-call/text opens the agent's own phone, and do-not-call / no-text flags disable those buttons.
- **Data boundary:** prospects hold contact details and short notes only. Policy details, health, financial or ID information belong in carrier systems, not here. Because prospect contact details are personal information, have someone familiar with your state's insurance data-security rules review this before agencies load real consumer data.

## Run it locally

Requirements: Python 3.12+ and PostgreSQL 16 (or Docker).

```bash
# Option A — Docker (simplest)
docker compose -f docker-compose.dev.yml up --build
# open http://localhost:8000 and click "Create an organization"
```

```bash
# Option B — Python directly (e.g. on Windows with a local Postgres)
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
# Create an empty database, then point the two URLs at it:
export MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/teampath
export DATABASE_URL=postgresql+psycopg://teampath_app:teampath_app@localhost:5432/teampath
export REQUIRE_ORG_APPROVAL=false
alembic upgrade head                                # also creates the teampath_app role
uvicorn teampath.web.main:app --reload --port 8000
python -m teampath.worker.main                      # in a second terminal (previews, escalations)
```

**Updating to new code:** pending database changes are applied automatically when the web app starts (`AUTO_MIGRATE`, on by default outside production). To apply them by hand, run `alembic upgrade head`. In production Docker the container does this before starting, and the app refuses to start against an out-of-date database rather than failing later.

On **Windows (PowerShell)**, put the settings in a `.env` file in the project folder (both the app and `alembic` read it) instead of `export`:

```
MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:YOURPASSWORD@localhost:5432/teampath
DATABASE_URL=postgresql+psycopg://teampath_app:teampath_app@localhost:5432/teampath
REQUIRE_ORG_APPROVAL=false
```

Without SMTP settings, development mode writes emails to the log and shows invitation links on screen, so you can test the whole flow locally.

**Make yourself a platform admin** (to approve organizations):

```bash
python -m teampath.cli create-platform-admin you@example.com "Your Name"
# in Docker: docker compose exec app /entrypoint.sh cli create-platform-admin you@example.com "Your Name"
```

## Deploy to your VPS (Traefik + GitHub Actions)

This follows the same pattern as your other Flet apps: GitHub → GHCR → VPS.

1. **Push this folder to a new GitHub repository** (e.g. `sukarnar/teampath`).
2. **On the VPS**, create a deploy directory with `docker-compose.yml`, `scripts/` and a `.env` based on `.env.example`:
   ```bash
   mkdir -p /opt/teampath && cd /opt/teampath
   # copy docker-compose.yml, .env.example and scripts/ here
   cp .env.example .env && chmod 600 .env && nano .env
   ```
   Fill in the domain, generate the secrets (commands are at the top of the file), and set SMTP — production refuses to start with default secrets. Match `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT` and `TRAEFIK_CERTRESOLVER` to your existing Traefik setup. Point a DNS record for `APP_DOMAIN` at the server.
3. **Add repository secrets** in GitHub → Settings → Secrets and variables → Actions:
   `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_APP_DIR` (`/opt/teampath`), and `GHCR_READ_TOKEN` (a token with `read:packages`).
   Optionally create a `production` environment with required reviewers so deploys wait for approval.
4. **Push to `main`.** The workflow runs the tests (including the cross-tenant isolation tests), checks dependencies with `pip-audit`, builds both images, scans them with Trivy, pushes them to GHCR, and runs `docker compose up -d` on the server. Migrations run automatically when the app container starts.
5. **First sign-in:** create the platform admin with the CLI command above, sign in, and turn on two-step verification.
6. **Backups:** add `scripts/backup.sh` to cron (instructions inside), copy `backups/` off the server, and test `scripts/restore.sh` once on a scratch server.
7. **Antivirus (recommended):** `docker compose --profile av up -d` and set `CLAMD_HOST=clamav`.

### Server hardening checklist

- Firewall: allow only 22 (ideally restricted to your IP), 80 and 443. `ufw` works well.
- SSH: keys only, no root login, `fail2ban`.
- `unattended-upgrades` for security patches.
- Postgres is on an internal Docker network and never published.
- Before onboarding your first real agency, get an external penetration test.

## Project layout

```
teampath/
  config.py          settings from environment
  db.py              engine + tenant/user/system sessions (sets the RLS context)
  models.py          SQLAlchemy models
  security.py        Argon2, tokens, TOTP, encryption at rest, signed URLs
  services/          all business rules and authorization (auth, orgs, content, paths,
                     qa, activity, goals, updates, teammap, notify, platform)
  worker/            background jobs: scanning, previews, logos, momentum, escalation
  web/               FastAPI host: health, signed files, uploads, Flet mount, security headers
  ui/                Flet UI: app shell, routing, theme, views/
migrations/          Alembic migration + row-level security policies (rls.py)
tests/               service tests, isolation tests, worker tests, and end-to-end UI tests
docker/, Dockerfile, docker-compose*.yml, .github/workflows/
```

## Tests

```bash
pytest -q
```

The suite (75 tests) creates a throwaway database and covers the following:

- **Tenant and hierarchy isolation:** every table, sibling branches, private data and append-only audit.
- **Authentication:** lockout, MFA, sessions, password reset and invitation rules.
- **Feature logic:** paths and sign-off, Q&A routing and escalation, goals, updates, team map, moves and releases, prospects, delegation, and notes (privacy, team events, sharing audiences, snapshots, takeaways, photo cleaning).
- **Worker:** document previews and rejection of disguised files.
- **End-to-end UI tests:** the real server runs with a headless Flet client that drives every page as owner, leader and agent at desktop and phone widths, through the following flows:
  - MFA enrolment
  - Invitations
  - Recruiting
  - Activity logging
  - Questions
  - Updates and feedback
  - Uploads, previews and watermarks
  - Branding
  - Live updates
  - Idle timeout

## Roadmap ideas

- Licensing & CE tracker with expiry reminders (insurance module)
- Content approval workflow for compliance
- Announcements that cascade down the tree, with required acknowledgment
- Events / weekly training calls with attendance
- Notes phase 2: AI summaries, “clean up for sharing”, team digest per event (self-hosted or hosted model)
- AI assistant over the content library and FAQs (local LLMs, as in DataBridge), with mentor-reviewed draft answers
- Passkeys (WebAuthn), subdomains per organization, custom domains
