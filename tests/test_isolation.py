"""The most important tests: one tenant (or branch) can never read another's data."""

import pytest
from sqlalchemy import text

from teampath.context import NotFound, PermissionDenied
from teampath.db import tenant_session
from teampath.models import TENANT_TABLES
from teampath.services import activity, content, goals, orgs, paths, qa, teammap, updates


def populate(world):
    owner = world.org("Alpha Agency")
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    pid = paths.create_path(owner, "New agent onboarding")
    paths.save_step(owner, pid, title="Get licensed", requires_signoff=True)
    paths.publish(owner, pid)
    paths.enroll(owner, pid, [agent.user_id])
    cid = content.create_item(owner, title="Script", kind="text", body_md="Hello")
    t = activity.tiles(agent)[0]
    activity.log(agent, t.type_id, 3)
    goals.create_goal(agent, "10 apps this month")
    qa.ask(agent, "How do I get appointed?", "Which carriers first?")
    updates.send_update(agent, wins="First appointment set")
    teammap.toggle_pin(leader, agent.user_id)
    teammap.save_note(leader, agent.user_id, ["needs coaching"], "private note")
    teammap.save_layout(leader, {"positions": {"x": [0.1, 0.2]}})
    teammap.share_opportunity(agent, leader.user_id, "Referral from event")
    return owner, leader, agent, cid


def test_every_tenant_table_is_isolated(world):
    owner_a, leader_a, agent_a, _ = populate(world)
    owner_b = world.org("Beta Agency")
    with tenant_session(owner_b) as db:
        for table in TENANT_TABLES:
            n = db.execute(text(f"select count(*) from {table} where org_id = :a"), {"a": owner_a.org_id}).scalar()
            assert n == 0, f"{table} leaked {n} rows across tenants"
        assert db.execute(text("select count(*) from users where id = :u"), {"u": agent_a.user_id}).scalar() == 0
        assert db.execute(text("select count(*) from organizations where id = :o"), {"o": owner_a.org_id}).scalar() == 0
        assert db.execute(text("select count(*) from audit_log where org_id = :o"), {"o": owner_a.org_id}).scalar() == 0


def test_tables_with_org_id_all_have_rls(world):
    with tenant_session(world.org()) as db:
        missing = db.execute(text(
            "select c.table_name from information_schema.columns c join pg_tables t on t.tablename = c.table_name "
            "where c.column_name = 'org_id' and t.schemaname = 'public' and not t.rowsecurity")).scalars().all()
    assert missing == []


def test_cannot_insert_into_another_tenant(world):
    owner_a = world.org("Org A")
    owner_b = world.org("Org B")
    with pytest.raises(Exception):
        with tenant_session(owner_b) as db:
            db.execute(text("insert into faq_entries (org_id, target_type, question, answer, created_by) "
                            "values (:o, 'general', 'q', 'a', :u)"), {"o": owner_a.org_id, "u": owner_b.user_id})


def test_sibling_branches_are_hidden(world):
    owner, leader, agent, _ = populate(world)
    other_leader = world.member(owner, "Otto Other", role="leader")
    # Other leader sees none of the first branch's personal data.
    assert agent.user_id not in [p.user_id for p in orgs.list_people(other_leader, include_upline=True)]
    with tenant_session(other_leader) as db:
        for table, col in [("activity_logs", "user_id"), ("goals", "user_id"), ("threads", "author_id"),
                           ("progress_updates", "user_id"), ("enrollments", "user_id"), ("step_progress", "user_id")]:
            n = db.execute(text(f"select count(*) from {table} where {col} = :u"), {"u": agent.user_id}).scalar()
            assert n == 0, f"{table} visible to sibling branch"
    with pytest.raises((PermissionDenied, NotFound)):
        teammap.build_tree(other_leader, agent.user_id)


def test_upline_and_admin_can_see_downline(world):
    owner, leader, agent, _ = populate(world)
    assert any(n.user_id == agent.user_id for n in teammap.flatten(teammap.build_tree(leader)))
    assert len(goals.list_goals(leader, agent.user_id)) == 1
    assert len(goals.list_goals(owner, agent.user_id)) == 1
    assert any(u.user_id == agent.user_id for u in updates.leader_inbox(leader))


def test_downline_cannot_see_upline_private_data(world):
    owner, leader, agent, _ = populate(world)
    with tenant_session(agent) as db:
        for table in ("person_notes", "map_layouts", "focus_pins"):
            assert db.execute(text(f"select count(*) from {table}")).scalar() == 0, table
        # The agent can see their leader exists (upline) but not the leader's goals/activity.
        assert db.execute(text("select count(*) from goals where user_id = :u"), {"u": leader.user_id}).scalar() == 0
    with pytest.raises(PermissionDenied):
        teammap.build_tree(agent, leader.user_id)


def test_private_notes_hidden_even_from_admins(world):
    owner, leader, agent, _ = populate(world)
    with tenant_session(owner) as db:
        assert db.execute(text("select count(*) from person_notes")).scalar() == 0


def test_activity_visibility_setting(world):
    owner, leader, agent, _ = populate(world)
    orgs.update_settings(owner, activity_visibility="shared")
    leader = world.ctx(leader.user_email, owner.org_id)  # context picks up the new setting
    with tenant_session(leader) as db:
        assert db.execute(text("select count(*) from activity_logs where user_id = :u"), {"u": agent.user_id}).scalar() == 0
    with tenant_session(owner) as db:  # admins still see it
        assert db.execute(text("select count(*) from activity_logs where user_id = :u"), {"u": agent.user_id}).scalar() > 0


def test_team_scoped_content_hidden_outside_branch(world):
    owner = world.org()
    leader = world.member(owner, "Lee", role="leader")
    agent = world.member(leader, "Ann", parent=leader)
    other = world.member(owner, "Otto", role="leader")
    cid = content.create_item(leader, title="Our team script", kind="text", body_md="secret sauce")
    assert content.open_item(agent, cid).title == "Our team script"
    with pytest.raises(NotFound):
        content.open_item(other, cid)
    assert content.open_item(owner, cid)


def test_audit_log_is_append_only(world):
    owner = world.org()
    with pytest.raises(Exception):
        with tenant_session(owner) as db:
            db.execute(text("delete from audit_log"))
