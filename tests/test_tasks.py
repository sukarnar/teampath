import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from teampath import mailer
from teampath.context import NotFound, PermissionDenied, ValidationError
from teampath.db import system_session, tenant_session
from teampath.models import Task
from teampath.services import activity, auth, notify, orgs, teammap
from teampath.services import prospects as P
from teampath.services import tasks as T
from conftest import PASSWORD


def setup(world):
    owner = world.org()
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    email = f"asst-{uuid.uuid4().hex[:8]}@example.com"
    raw = orgs.invite(agent, email, role="assistant")
    auth.accept_invitation(raw, name="Sam Assistant", password=PASSWORD)
    asst = world.ctx(email, owner.org_id)
    return owner, leader, agent, asst


def test_assistant_invite_links_and_stays_off_team_map(world):
    owner, leader, agent, asst = setup(world)
    assert asst.role == "assistant"
    assert [h.name for h in T.my_assistants(agent)] == ["Sam Assistant"]
    assert [h.name for h in T.my_agents(asst)] == ["Ann Agent"]
    # Inviting an assistant doesn't make the agent a leader, and assistants aren't on team maps.
    assert world.ctx(agent.user_email, owner.org_id).role == "member"
    assert all(n.name != "Sam Assistant" for n in teammap.flatten(teammap.build_tree(leader)))
    with pytest.raises(PermissionDenied):
        orgs.invite(asst, "x@example.com")


def test_delegation_privacy_and_flow(world):
    owner, leader, agent, asst = setup(world)
    pid = P.create(agent, name="Maria Lopez", phone="9195550142", details="Private: going through a divorce")
    tid = T.create(agent, title="Confirm Tuesday appointment", details="Ask if her husband will join",
                   assignee_id=asst.user_id, due_on=agent.today(), prospect_id=pid)
    # Only the agent and the assistant see it; not the leader, not admins.
    for ctx in (leader, owner):
        with pytest.raises(NotFound):
            T.get(ctx, tid)
        with tenant_session(ctx) as db:
            assert db.execute(text("select count(*) from tasks")).scalar() == 0
    assert any(n.kind == "task_assigned" for n in notify.my_notifications(asst))
    # Details are encrypted at rest.
    with system_session("test") as db:
        assert "husband" not in (db.get(Task, tid).details_enc or "")
    # The assistant sees the prospect's contact details, but not the agent's private notes.
    d = P.get_for_task(asst, tid)
    assert d.phone == "9195550142" and d.details is None
    assert T.get(asst, tid).details == "Ask if her husband will join"
    with pytest.raises(NotFound):
        P.get(asst, pid)  # no general access to the agent's prospects
    assert P.search(asst) == []
    # The assistant logs a call through the task; it isn't counted as the agent's activity.
    with pytest.raises(ValidationError):
        P.log_touch(asst, pid, kind="call", outcome="won", task_id=tid)  # closing stays with the agent
    P.log_touch(asst, pid, kind="call", outcome="appointment", note="Confirmed, both attending",
                next_on=agent.today() + timedelta(days=3), task_id=tid)
    dd = P.get(agent, pid)
    assert dd.stage == "appointment" and dd.touches[0].by_name == "Sam Assistant"
    assert {t.label: t.today for t in activity.tiles(agent)}["Dials"] == 0
    # Comments go both ways with notifications.
    T.comment(asst, tid, "She asked to move it to 7pm")
    assert any(n.kind == "task_comment" for n in notify.my_notifications(agent))
    T.comment(agent, tid, "7pm is fine")
    assert [c.body for c in T.get(asst, tid).thread] == ["She asked to move it to 7pm", "7pm is fine"]
    # Done -> the agent is told, and the assistant's access to the prospect ends.
    with pytest.raises(PermissionDenied):
        T.set_status(asst, tid, "cancelled")
    T.set_status(asst, tid, "done", "All set for Tuesday 7pm")
    assert any(n.kind == "task_done" for n in notify.my_notifications(agent))
    assert T.get(agent, tid).completion_note == "All set for Tuesday 7pm"
    assert P.get_for_task(asst, tid) is None
    with pytest.raises(NotFound):
        P.log_touch(asst, pid, kind="call", outcome="talked", next_on=agent.today(), task_id=tid)


def test_only_own_assistants_and_own_prospects(world):
    owner, leader, agent, asst = setup(world)
    other = world.member(leader, "Bob Agent", parent=leader)
    with pytest.raises(PermissionDenied):
        T.create(other, title="Not yours", assignee_id=asst.user_id)
    bob_p = P.create(other, name="Bob's lead")
    with pytest.raises(NotFound):
        T.create(agent, title="x", assignee_id=asst.user_id, prospect_id=bob_p)
    # The database enforces it too.
    with pytest.raises(Exception):
        with tenant_session(other) as db:
            db.execute(text("insert into tasks (org_id, owner_id, assignee_id, title) values (:o, :me, :a, 'x')"),
                       {"o": other.org_id, "me": other.user_id, "a": asst.user_id})
    # Personal to-dos work for anyone.
    T.create(other, title="Renew my license")
    assert [r.title for r in T.assigned_to_me(other)] == ["Renew my license"]


def test_link_unlink_and_release_hand_back(world):
    owner, leader, agent, asst = setup(world)
    member = world.member(leader, "Mia Member", parent=leader)
    T.link_assistant(leader, member.user_id)  # someone in the leader's own team
    with pytest.raises(PermissionDenied):
        T.link_assistant(agent, leader.user_id)  # not in the agent's team
    tid = T.create(leader, title="Prep the training call", assignee_id=member.user_id)
    assert T.unlink_assistant(leader, member.user_id) == 1
    t = T.get(leader, tid)
    assert t.assignee_id == leader.user_id and t.status == "open"
    # Releasing an assistant hands their open tasks back.
    tid2 = T.create(agent, title="Mail the welcome kit", assignee_id=asst.user_id)
    m = next(p for p in orgs.list_people(owner) if p.user_id == asst.user_id)
    orgs.release_member(owner, m.membership_id)
    assert T.get(agent, tid2).assignee_id == agent.user_id
    assert T.my_assistants(agent) == []
    assert any(n.kind == "task_returned" for n in notify.my_notifications(agent))


def test_summary_counts(world):
    owner, leader, agent, asst = setup(world)
    today = agent.today()
    T.create(agent, title="a", assignee_id=asst.user_id, due_on=today)
    t2 = T.create(agent, title="b", assignee_id=asst.user_id, due_on=today)
    T.create(agent, title="mine", due_on=today)
    T.set_status(asst, t2, "done")
    s = T.summary(agent)
    assert (s.due_today, s.delegated_open, s.delegated_done_today) == (1, 1, 1)
    s = T.summary(asst)
    assert (s.due_today, s.open) == (1, 1)
    mailer.OUTBOX.clear()
