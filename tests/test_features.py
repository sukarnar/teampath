from datetime import date, timedelta

import pytest

from teampath.context import PermissionDenied, ValidationError
from teampath.db import tenant_session
from teampath.models import Membership
from teampath.services import activity, goals, notify, orgs, paths, qa, teammap, updates


def team(world):
    owner = world.org()
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    return owner, leader, agent


def test_path_sequence_signoff_and_versions(world):
    owner, leader, agent = team(world)
    pid = paths.create_path(owner, "Onboarding")
    paths.save_step(owner, pid, title="Read handbook", step_type="read")
    paths.save_step(owner, pid, title="Get licensed", requires_signoff=True)
    assert paths.publish(owner, pid) == 1
    assert paths.enroll(leader, pid, [agent.user_id]) == 1
    e = paths.my_enrollments(agent)[0]
    assert [s.locked for s in e.steps] == [False, True]
    with pytest.raises(ValidationError):
        paths.complete_step(agent, e.steps[1].id)
    assert paths.complete_step(agent, e.steps[0].id) == "done"
    assert paths.complete_step(agent, e.steps[1].id) == "pending_signoff"
    pending = paths.pending_signoffs(leader)
    assert len(pending) == 1
    with pytest.raises(PermissionDenied):
        paths.sign_off(agent, pending[0][0].id)
    paths.sign_off(leader, pending[0][0].id)
    e = paths.my_enrollments(agent)[0]
    assert e.status == "completed" and e.percent == 100
    # New version: completed steps carry over by step key.
    paths.save_step(owner, pid, title="Shadow a call")
    assert paths.publish(owner, pid) == 2
    paths.migrate_enrollment(owner, e.id)
    e = paths.my_enrollments(agent)[0]
    assert e.version_no == 2 and e.done == 2 and e.total == 3


def test_leader_cannot_enroll_outside_branch(world):
    owner, leader, agent = team(world)
    other = world.member(owner, "Otto", role="leader")
    pid = paths.create_path(owner, "P")
    paths.save_step(owner, pid, title="S")
    paths.publish(owner, pid)
    with pytest.raises(Exception):
        paths.enroll(leader, pid, [other.user_id])


def test_question_routes_to_leader_and_faq(world):
    owner, leader, agent = team(world)
    tid = qa.ask(agent, "Which carrier first?", "New to this")
    assert qa.get_thread(agent, tid).assigned_to_name == "Lee Leader"
    assert [t.id for t in qa.inbox(leader)] == [tid]
    assert any(n.kind == "question" for n in notify.my_notifications(leader))
    qa.reply(leader, tid, "Start with the two carriers we're appointed with.")
    t = qa.get_thread(agent, tid)
    assert t.status == "answered" and t.comments[-1].is_answer
    qa.promote_to_faq(leader, tid, "Which carrier first?", "Start with our two appointed carriers.")
    assert len(qa.faq_for(agent, "general", None)) == 1
    qa.resolve(agent, tid)
    assert qa.inbox(leader) == []


def test_activity_momentum_and_tiles(world):
    owner, leader, agent = team(world)
    tile = activity.tiles(agent)[0]
    activity.log(agent, tile.type_id, 5)
    activity.log(agent, tile.type_id, -2)
    activity.log(agent, tile.type_id, -10)  # can't go below zero
    t = [x for x in activity.tiles(agent) if x.type_id == tile.type_id][0]
    assert t.today == 0
    activity.log(agent, tile.type_id, 4)
    assert [x for x in activity.tiles(agent) if x.type_id == tile.type_id][0].today == 4
    with pytest.raises(ValidationError):
        activity.log(agent, tile.type_id, 1, day=date.today() + timedelta(days=1))
    node = [n for n in teammap.flatten(teammap.build_tree(leader)) if n.user_id == agent.user_id][0]
    assert node.momentum == "active" and node.week_activity == 4


def test_momentum_rules():
    today = date(2026, 10, 9)
    joined = date(2026, 1, 1)
    assert activity.momentum_status(0, 0, None, today - timedelta(days=3), today) == "new"
    assert activity.momentum_status(0, 10, today - timedelta(days=9), joined, today) == "inactive"
    assert activity.momentum_status(10, 40, today, joined, today) == "slowing"
    assert activity.momentum_status(30, 40, today - timedelta(days=5), joined, today) == "slowing"
    assert activity.momentum_status(40, 40, today, joined, today) == "active"


def test_updates_go_to_leader_with_feedback(world):
    owner, leader, agent = team(world)
    recips = updates.possible_recipients(agent)
    assert recips[0][0] == leader.user_id and recips[0][2]
    with pytest.raises(PermissionDenied):
        updates.send_update(agent, wins="x", recipient_ids=[agent.user_id])
    uid = updates.send_update(agent, wins="3 appointments", help_wanted="Objection handling")
    inbox = updates.leader_inbox(leader, awaiting_only=True)
    assert [u.id for u in inbox] == [uid]
    updates.add_feedback(leader, uid, "Great week — let's role-play objections Tuesday", kind="coaching")
    assert updates.leader_inbox(leader, awaiting_only=True) == []
    u = updates.get_update(agent, uid)
    assert u.feedback[0].kind == "coaching" and u.snapshot["activity"]


def test_goals_need_leader_approval(world):
    owner, leader, agent = team(world)
    gid = goals.create_goal(agent, "Get appointed with 3 carriers", horizon="30")
    with pytest.raises(PermissionDenied):
        goals.update_goal(agent, gid, status="active")
    goals.update_goal(leader, gid, status="active")
    goals.update_goal(agent, gid, progress=100)
    assert goals.list_goals(agent)[0].status == "done"


def test_team_map_pack_pins_and_focus(world):
    owner, leader, agent = team(world)
    a2 = world.member(leader, "Bob Agent", parent=leader)
    world.member(agent, "Cat Recruit", parent=agent)
    root = teammap.build_tree(leader)
    assert root.team_size == 3
    bubbles = teammap.pack(root)
    assert {b.node.name for b in bubbles} == {"Ann Agent", "Bob Agent", "Cat Recruit"}
    assert all(0 <= b.x <= 1 and 0 <= b.y <= 1 and b.r > 0 for b in bubbles)
    assert teammap.toggle_pin(leader, a2.user_id) is True
    focus = teammap.my_focus(leader)
    assert [f.node.user_id for f in focus] == [a2.user_id] and focus[0].suggestion
    with pytest.raises(PermissionDenied):
        teammap.toggle_pin(agent, leader.user_id)  # can't pin upline


def test_move_and_release_member(world):
    owner, leader, agent = team(world)
    recruit = world.member(agent, "Cat", parent=agent)
    other = world.member(owner, "Otto", role="leader")
    orgs.move_member(owner, agent.membership_id, other.membership_id)
    with tenant_session(owner) as db:
        r = db.get(Membership, recruit.membership_id)
        o = db.get(Membership, other.membership_id)
        assert r.path.startswith(o.path + ".")
    with pytest.raises(ValidationError):
        orgs.move_member(owner, other.membership_id, recruit.membership_id)
    orgs.release_member(owner, agent.membership_id, note="Joined another agency")
    with tenant_session(owner) as db:
        r = db.get(Membership, recruit.membership_id)
        assert r.parent_id == other.membership_id
        assert db.get(Membership, agent.membership_id).status == "left"


def test_opportunity_sharing(world):
    owner, leader, agent = team(world)
    oid = teammap.share_opportunity(leader, agent.user_id, "Warm referral – small business")
    teammap.update_opportunity(agent, oid, "won", "Closed")
    rows = teammap.list_opportunities(leader)
    assert rows[0][0].status == "won"


def test_goal_approval_queue(world):
    owner, leader, agent = team(world)
    recruit = world.member(agent, "Rex Recruit", parent=agent)  # agent is now a leader too
    other = world.member(owner, "Otto Other", role="leader")
    gid = goals.create_goal(recruit, "Book 10 appointments")
    # Everyone above the recruit sees it; a leader in another branch does not.
    for ctx in (world.ctx(agent.user_email, owner.org_id), leader, owner):
        assert [g.id for g, _ in goals.pending_approvals(ctx)] == [gid]
    assert goals.pending_approvals(other) == []
    assert goals.pending_approvals(recruit) == []
    # The direct leader is notified and pointed at the queue.
    direct = world.ctx(agent.user_email, owner.org_id)
    n = [x for x in notify.my_notifications(direct) if x.kind == "goal_proposed"]
    assert n and n[0].route == "/goals?tab=approve"
    # Map flags it.
    node = [x for x in teammap.flatten(teammap.build_tree(leader)) if x.user_id == recruit.user_id][0]
    assert node.proposed_goals == 1 and node.needs_attention
    # Can't self-approve; leader sends it back; recruit re-proposes; leader approves.
    with pytest.raises(PermissionDenied):
        goals.update_goal(recruit, gid, status="active")
    goals.decline_goal(leader, gid, "Make it measurable by week")
    assert goals.pending_approvals(leader) == []
    assert any(x.kind == "goal_declined" and x.body for x in notify.my_notifications(recruit))
    goals.update_goal(recruit, gid, status="proposed")
    assert len(goals.pending_approvals(leader)) == 1
    goals.update_goal(leader, gid, status="active")
    assert goals.list_goals(recruit)[0].status == "active"
    assert goals.pending_approvals(owner) == []


def test_goal_at_top_of_org_needs_no_approval(world):
    owner = world.org()
    goals.create_goal(owner, "Grow the agency to 50 agents")
    assert goals.list_goals(owner)[0].status == "active"
