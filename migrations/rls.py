"""Row-level security: the database-side safety net for tenant and hierarchy isolation.

The application connects as `teampath_app`, which does not own the tables,
so these policies always apply to it. Each transaction sets:

    app.user_id, app.org_id, app.role, app.user_path, app.activity_upline, app.bypass

(see teampath/db.py). `app.bypass` is used only by teampath.db.system_session
for authentication lookups, platform-admin screens and background jobs.
"""

from alembic import op

from teampath.config import get_settings

APP_ROLE = "teampath_app"

FUNCTIONS = r"""
create or replace function app_setting(k text) returns text
  language sql stable as $$ select nullif(current_setting(k, true), '') $$;
create or replace function app_bypass() returns boolean
  language sql stable as $$ select coalesce(app_setting('app.bypass'), 'off') = 'on' $$;
create or replace function app_user() returns uuid
  language sql stable as $$ select app_setting('app.user_id')::uuid $$;
create or replace function app_org() returns uuid
  language sql stable as $$ select app_setting('app.org_id')::uuid $$;
create or replace function app_role() returns text
  language sql stable as $$ select app_setting('app.role') $$;
create or replace function app_path() returns ltree
  language sql stable as $$ select app_setting('app.user_path')::ltree $$;
create or replace function app_is_admin() returns boolean
  language sql stable as $$ select coalesce(app_role() in ('owner', 'admin'), false) $$;
create or replace function app_activity_upline() returns boolean
  language sql stable as $$ select coalesce(app_setting('app.activity_upline'), 'on') = 'on' $$;

-- SECURITY DEFINER helpers read memberships without recursing into its own policy.
create or replace function app_in_my_branch(target uuid) returns boolean
  language sql stable security definer set search_path = public as $$
  select app_path() is not null and exists (
    select 1 from memberships m
    where m.org_id = app_org() and m.user_id = target and m.path <@ app_path())
$$;
create or replace function app_shares_org(target uuid) returns boolean
  language sql stable security definer set search_path = public as $$
  select exists (select 1 from memberships m where m.org_id = app_org() and m.user_id = target)
$$;
create or replace function app_is_member_of(target_org uuid) returns boolean
  language sql stable security definer set search_path = public as $$
  select exists (select 1 from memberships m
                 where m.org_id = target_org and m.user_id = app_user() and m.status = 'active')
$$;
create or replace function app_can_see_person(target uuid) returns boolean
  language sql stable as $$
  select app_bypass() or target = app_user() or app_is_admin() or app_in_my_branch(target)
$$;
"""

PERSON = {
    "enrollments": "user_id",
    "step_progress": "user_id",
    "goals": "user_id",
    "momentum": "user_id",
    "comments": "thread_author_id",
}
PRIVATE = {"person_notes": "author_id", "map_layouts": "owner_id", "focus_pins": "owner_id"}


def _enable(table):
    op.execute(f"alter table {table} enable row level security")


def apply_rls():
    s = get_settings()
    op.execute(
        f"""
        do $$ begin
          if not exists (select from pg_roles where rolname = '{APP_ROLE}') then
            create role {APP_ROLE} login password '{s.app_db_password.replace("'", "''")}';
          end if;
        end $$;
        """
    )
    op.execute(FUNCTIONS)

    tenant = "app_bypass() or org_id = app_org()"

    # Org-wide tables: any member of the organization.
    for t in ("activity_types", "faq_entries", "files"):
        _enable(t)
        op.execute(f"create policy tenant on {t} using ({tenant}) with check ({tenant})")

    # Branding: readable by members, changed by admins.
    _enable("org_branding")
    op.execute(f"create policy read on org_branding for select using ({tenant})")
    op.execute(
        "create policy write on org_branding for all using (app_bypass() or (org_id = app_org() and app_is_admin())) "
        "with check (app_bypass() or (org_id = app_org() and app_is_admin()))"
    )

    # Content and paths can be scoped to one branch of the hierarchy.
    scoped = (
        "app_bypass() or (org_id = app_org() and (scope_path is null or app_is_admin() "
        "or app_path() <@ scope_path or scope_path <@ app_path()))"
    )
    for t in ("content_items", "paths"):
        _enable(t)
        op.execute(f"create policy scoped on {t} using ({scoped}) with check ({scoped})")

    # Children inherit visibility from their (RLS-protected) parents.
    child = {
        "content_versions": "exists (select 1 from content_items c where c.id = item_id)",
        "path_versions": "exists (select 1 from paths p where p.id = path_id)",
        "path_steps": "exists (select 1 from path_versions v where v.id = version_id)",
    }
    for t, cond in child.items():
        _enable(t)
        op.execute(
            f"create policy via_parent on {t} using (app_bypass() or (org_id = app_org() and {cond})) "
            f"with check (app_bypass() or (org_id = app_org() and {cond}))"
        )

    # Person-scoped: yourself, admins, and leaders above you in the hierarchy.
    for t, col in PERSON.items():
        _enable(t)
        cond = f"app_bypass() or (org_id = app_org() and app_can_see_person({col}))"
        op.execute(f"create policy person on {t} using ({cond}) with check ({cond})")

    # Threads: author's upline, admins, and whoever it is assigned to.
    _enable("threads")
    cond = "app_bypass() or (org_id = app_org() and (app_can_see_person(author_id) or assigned_to = app_user()))"
    op.execute(f"create policy person on threads using ({cond}) with check ({cond})")
    _enable("comments")  # policy created above via PERSON; add assignee read access
    op.execute(
        "create policy assignee on comments for select using (org_id = app_org() and exists "
        "(select 1 from threads t where t.id = thread_id and t.assigned_to = app_user()))"
    )
    op.execute(
        "create policy assignee_insert on comments for insert with check (org_id = app_org() and exists "
        "(select 1 from threads t where t.id = thread_id and t.assigned_to = app_user()))"
    )

    # Daily activity: upline visibility depends on the organization's setting.
    _enable("activity_logs")
    cond = (
        "app_bypass() or (org_id = app_org() and (user_id = app_user() or app_is_admin() "
        "or (app_activity_upline() and app_in_my_branch(user_id))))"
    )
    op.execute(f"create policy person on activity_logs using ({cond}) with check ({cond})")

    # Progress updates: owner, recipients, upline, admins.
    _enable("progress_updates")
    cond = "app_bypass() or (org_id = app_org() and (app_can_see_person(user_id) or app_user() = any(recipients)))"
    op.execute(f"create policy person on progress_updates using ({cond}) with check ({cond})")
    _enable("feedback")
    cond = (
        "app_bypass() or (org_id = app_org() and (app_can_see_person(update_owner_id) or author_id = app_user() "
        "or exists (select 1 from progress_updates u where u.id = update_id)))"
    )
    op.execute(f"create policy person on feedback using ({cond}) with check ({cond})")

    # Private to the author: personal map layout, notes and focus pins.
    for t, col in PRIVATE.items():
        _enable(t)
        cond = f"app_bypass() or (org_id = app_org() and {col} = app_user())"
        op.execute(f"create policy private on {t} using ({cond}) with check ({cond})")

    _enable("opportunity_shares")
    cond = (
        "app_bypass() or (org_id = app_org() and (from_user_id = app_user() or to_user_id = app_user() "
        "or app_is_admin() or app_in_my_branch(from_user_id) or app_in_my_branch(to_user_id)))"
    )
    op.execute(f"create policy person on opportunity_shares using ({cond}) with check ({cond})")

    _enable("notifications")
    op.execute(
        "create policy own on notifications for select using (app_bypass() or (org_id = app_org() and user_id = app_user()))"
    )
    op.execute(
        "create policy own_update on notifications for update using (app_bypass() or (org_id = app_org() and user_id = app_user()))"
    )
    op.execute(
        "create policy own_delete on notifications for delete using (app_bypass() or (org_id = app_org() and user_id = app_user()))"
    )
    op.execute(f"create policy send on notifications for insert with check ({tenant})")

    # Memberships: yourself (in any org), your upline chain, your downline, admins.
    _enable("memberships")
    op.execute(
        "create policy visible on memberships for select using (app_bypass() or user_id = app_user() or "
        "(org_id = app_org() and (app_is_admin() or path <@ app_path() or app_path() <@ path)))"
    )
    op.execute(
        "create policy manage on memberships for all using (app_bypass() or (org_id = app_org() and "
        "(app_is_admin() or (path <@ app_path() and path <> app_path())))) "
        "with check (app_bypass() or (org_id = app_org() and (app_is_admin() or path <@ app_path())))"
    )

    _enable("invitations")
    cond = "app_bypass() or (org_id = app_org() and (app_is_admin() or created_by = app_user()))"
    op.execute(f"create policy tenant on invitations using ({cond}) with check ({cond})")

    # Global tables.
    _enable("users")
    op.execute(
        "create policy visible on users for select using (app_bypass() or id = app_user() or app_shares_org(id))"
    )
    op.execute(
        "create policy self_update on users for update using (app_bypass() or id = app_user()) "
        "with check (app_bypass() or id = app_user())"
    )
    op.execute("create policy system_insert on users for insert with check (app_bypass())")
    op.execute("create policy system_delete on users for delete using (app_bypass())")

    _enable("auth_tokens")
    cond = "app_bypass() or user_id = app_user()"
    op.execute(f"create policy own on auth_tokens using ({cond}) with check ({cond})")

    _enable("organizations")
    op.execute(
        "create policy visible on organizations for select using (app_bypass() or id = app_org() or app_is_member_of(id))"
    )
    op.execute(
        "create policy admin_update on organizations for update using (app_bypass() or (id = app_org() and app_is_admin())) "
        "with check (app_bypass() or (id = app_org() and app_is_admin()))"
    )
    op.execute("create policy system_insert on organizations for insert with check (app_bypass())")
    op.execute("create policy system_delete on organizations for delete using (app_bypass())")

    _enable("audit_log")
    op.execute(
        "create policy read on audit_log for select using (app_bypass() or (org_id = app_org() and app_is_admin()))"
    )
    op.execute(
        "create policy write on audit_log for insert with check (app_bypass() or org_id is null or org_id = app_org())"
    )

    # Privileges for the application role.
    op.execute(f"grant usage on schema public to {APP_ROLE}")
    op.execute(f"grant select, insert, update, delete on all tables in schema public to {APP_ROLE}")
    op.execute(f"grant usage, select on all sequences in schema public to {APP_ROLE}")
    op.execute(f"revoke update, delete, truncate on audit_log from {APP_ROLE}")
    op.execute(f"revoke all on alembic_version from {APP_ROLE}")
    for fn in ("app_in_my_branch(uuid)", "app_shares_org(uuid)", "app_is_member_of(uuid)"):
        op.execute(f"revoke all on function {fn} from public")
        op.execute(f"grant execute on function {fn} to {APP_ROLE}")


def drop_rls():
    op.execute(
        "drop function if exists app_can_see_person(uuid), app_is_member_of(uuid), app_shares_org(uuid), "
        "app_in_my_branch(uuid) cascade"
    )
