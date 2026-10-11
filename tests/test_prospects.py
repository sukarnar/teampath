from datetime import timedelta

import pytest
from sqlalchemy import text, update

from teampath.context import NotFound, ValidationError
from teampath.db import system_session, tenant_session
from teampath.models import Prospect
from teampath.services import activity, orgs
from teampath.services import prospects as P


def team(world):
    owner = world.org()
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    return owner, leader, agent


def test_private_to_owner_and_encrypted(world):
    owner, leader, agent = team(world)
    pid = P.create(agent, name="Maria Lopez", phone="(919) 555-0142", email="Maria@Example.com",
                   details="Two kids, renews auto policy in March")
    # Leader and admins can't see it — the database refuses.
    for ctx in (leader, owner):
        with pytest.raises(NotFound):
            P.get(ctx, pid)
        assert P.search(ctx) == []
        with tenant_session(ctx) as db:
            assert db.execute(text("select count(*) from prospects")).scalar() == 0
    # Contact details and notes are encrypted at rest.
    with system_session("test") as db:
        raw = db.get(Prospect, pid)
        blob = " ".join(str(v) for v in (raw.phone_enc, raw.email_enc, raw.details_enc))
        assert "5550142" not in blob and "maria@" not in blob.lower() and "kids" not in blob
    d = P.get(agent, pid)
    assert d.phone == "9195550142" and d.email == "maria@example.com" and "kids" in d.details
    # Duplicates found by keyed hash, and search by phone works without decrypting.
    with pytest.raises(P.DuplicateProspect):
        P.create(agent, name="M. Lopez", phone="+1 919-555-0142")
    assert [r.id for r in P.search(agent, {"text": "919.555.0142"})] == [pid]
    # Another agent may have the same person — lists are per agent.
    other = world.member(leader, "Bob Agent", parent=leader)
    P.create(other, name="Maria Lopez", phone="919-555-0142")


def test_log_touch_moves_stage_requires_next_step_and_counts_activity(world):
    owner, leader, agent = team(world)
    pid = P.create(agent, name="Sam Lee", phone="9195550100", temperature="hot")
    today = agent.today()
    with pytest.raises(ValidationError):  # every open prospect must have a next step
        P.log_touch(agent, pid, kind="call", outcome="no_answer")
    P.log_touch(agent, pid, kind="call", outcome="no_answer", next_on=today + timedelta(days=1))
    assert P.get(agent, pid).stage == "new"
    r = P.log_touch(agent, pid, kind="call", outcome="appointment", note="Tue 6pm at his office",
                    next_on=today + timedelta(days=4), next_time="18:00")
    assert r.stage == "appointment" and set(r.counted) == {"Dials", "Contacts", "Appointments set"}
    d = P.get(agent, pid)
    assert d.next_action == "Appointment" and d.next_time == "18:00" and d.touches[0].note == "Tue 6pm at his office"
    # Stages only move forward from outcomes.
    P.log_touch(agent, pid, kind="text", outcome="talked", next_on=today + timedelta(days=2))
    assert P.get(agent, pid).stage == "appointment"
    P.log_touch(agent, pid, kind="meeting", outcome="met", next_on=today + timedelta(days=2))
    P.log_touch(agent, pid, kind="meeting", outcome="application", next_on=today + timedelta(days=7))
    r = P.log_touch(agent, pid, kind="call", outcome="won")
    assert r.closed and P.get(agent, pid).next_on is None
    tiles = {t.label: t.today for t in activity.tiles(agent)}
    assert tiles["Dials"] == 3 and tiles["Appointments held"] == 1 and tiles["Applications submitted"] == 1
    # Closed prospects drop out of follow-up lists.
    assert P.count(agent, {"due": "due"}) == 0


def test_recruit_pipeline_and_dnc(world):
    owner, leader, agent = team(world)
    pid = P.create(agent, name="Jo Recruit", kind="recruit", phone="9195550111", dnc=True)
    with pytest.raises(ValidationError):
        P.log_touch(agent, pid, kind="call", outcome="talked", next_on=agent.today())
    with pytest.raises(ValidationError):  # sales-only outcome
        P.log_touch(agent, pid, kind="meeting", outcome="application", next_on=agent.today())
    P.log_touch(agent, pid, kind="meeting", outcome="interviewed", next_on=agent.today())
    P.log_touch(agent, pid, kind="note", outcome="licensing", next_on=agent.today())
    assert P.get(agent, pid).stage == "licensing"
    assert [r.id for r in P.search(agent, P.BUILTIN["recruit_licensing"][1])] == [pid]
    tiles = {t.label: t.today for t in activity.tiles(agent)}
    assert tiles["Recruiting conversations"] == 1


def test_lists_and_focus_session(world):
    owner, leader, agent = team(world)
    today = agent.today()
    a = P.create(agent, name="Overdue Olga", next_on=today)
    b = P.create(agent, name="Today Tom", temperature="hot")
    c = P.create(agent, name="Later Lin", next_on=today + timedelta(days=10))
    with system_session("test") as db:
        db.execute(update(Prospect).where(Prospect.id == a).values(next_action_on=today - timedelta(days=2)))
    assert P.due_counts(agent) == (1, 1)
    infos = {i.id: i for i in P.lists(agent)}
    assert infos["overdue"].count == 1 and infos["due_today"].count == 1 and infos["hot"].count == 1
    sid = P.save_smart_list(agent, "Hot ones", {"temperature": ["hot"]})
    assert [r.id for r in P.search(agent, P.resolve_filter(agent, str(sid))[1])] == [b]
    # Focus list from overdue + today, worked one at a time.
    lid = P.create_focus_list(agent, "Saturday call block", [r.id for r in P.search(agent, {"due": "due"})])
    st = P.focus_state(agent, lid)
    assert st.total == 2 and st.current.name == "Overdue Olga"  # oldest first
    P.log_touch(agent, st.current.id, kind="call", outcome="voicemail", next_on=today + timedelta(days=2), list_id=lid)
    st = P.focus_state(agent, lid)
    assert st.done == 1 and st.current.name == "Today Tom"
    P.skip(agent, lid, st.current.id)
    st = P.focus_state(agent, lid)
    assert st.current is None and st.outcomes == {"voicemail": 1, "skipped": 1}
    assert next(i for i in P.lists(agent) if i.id == str(lid)).completed
    assert P.add_to_focus(agent, lid, [c]) == 1
    assert not next(i for i in P.lists(agent) if i.id == str(lid)).completed
    fid = P.focus_from(agent, "hot")
    assert P.focus_state(agent, fid).total == 1
    with pytest.raises(NotFound):
        P.focus_state(leader, lid)


def test_csv_import_and_export(world, tmp_path):
    owner, leader, agent = team(world)
    P.create(agent, name="Existing Eve", phone="9195550199")
    f = tmp_path / "leads.csv"
    f.write_text("First Name,Last Name,Mobile,E-mail,Lead Source,Tags,Notes\n"
                 "Al,Adams,919-555-0101,al@example.com,Event,expo;2026,Met at booth\n"
                 "Eve,Again,(919) 555-0199,,,,dup by phone\n"
                 ",,,,,,\n"
                 "Bad,Phone,12,,,,\n"
                 "Cy,Cole,,cy@example.com,referral,,\n", encoding="utf-8")
    prev = P.preview_csv(f)
    assert prev.rows == 4 and prev.columns["phone"] == "Mobile"
    res = P.import_csv(agent, f, tag="expo leads")
    assert (res.created, res.duplicates, res.skipped) == (2, 1, 1) and res.problems
    al = P.search(agent, {"text": "Al Adams"})[0]
    assert al.source == "event" and set(al.tags) == {"expo", "2026", "expo leads"}
    url = P.export_csv(agent)
    token = url.split("/exports/")[1].split("?")[0]
    q = dict(x.split("=") for x in url.split("?")[1].split("&"))
    assert P.verify_export(token, q["u"], int(q["e"]), q["s"])
    assert P.verify_export(token, str(leader.user_id), int(q["e"]), q["s"]) is None
    body = P.verify_export(token, q["u"], int(q["e"]), q["s"]).read_text(encoding="utf-8-sig")
    assert "Al Adams" in body and "(919) 555-0101" in body


def test_release_and_retention(world):
    owner, leader, agent = team(world)
    pid = P.create(agent, name="Kept Private")
    old = P.create(agent, name="Long Gone")
    orgs.release_member(owner, agent.membership_id, "Moved to another agency")
    with system_session("test") as db:
        p = db.get(Prospect, pid)
        assert p.owner_left_at is not None and p.owner_id == agent.user_id  # nobody else gets them
        db.execute(update(Prospect).where(Prospect.id == pid).values(owner_left_at=p.owner_left_at - timedelta(days=31)))
    other = world.member(leader, "Bob Agent", parent=leader)
    stale = P.create(other, name="Stale Sue")
    with system_session("test") as db:
        db.execute(update(Prospect).where(Prospect.id == stale).values(
            created_at=Prospect.created_at - timedelta(days=400), next_action_on=None))
    stats = P.apply_retention()
    assert stats["released_deleted"] >= 1 and stats["archived"] >= 1
    with system_session("test") as db:
        assert db.get(Prospect, pid) is None
        assert db.get(Prospect, old) is not None  # still within the 30-day grace period
        assert db.get(Prospect, stale).archived_at is not None
    assert P.search(other) == []
    assert [r.name for r in P.search(other, {"archived": True})] == ["Stale Sue"]
