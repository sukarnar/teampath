import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import text

from teampath.context import NotFound, PermissionDenied, ValidationError
from teampath.db import system_session, tenant_session
from teampath.models import Note, SharedNote, StoredFile
from teampath.services import auth, goals, notify, orgs
from teampath.services import notes as N
from teampath.services import tasks as T
from conftest import PASSWORD

BODY = """Opening keynote
KEY: Ask for referrals at every policy review
- ACTION: Call my top 10 clients this week
Q: How do we handle clients who already have term?
STAT: 60% of families are underinsured
just a plain line
"""


def setup(world):
    owner = world.org()
    leader = world.member(owner, "Lee Leader", role="leader")
    ann = world.member(leader, "Ann Agent", parent=leader)
    bob = world.member(leader, "Bob Agent", parent=leader)  # Ann's sibling
    cara = world.member(ann, "Cara Recruit", parent=ann)  # Ann's team
    email = f"asst-{uuid.uuid4().hex[:8]}@example.com"
    raw = orgs.invite(ann, email, role="assistant")
    auth.accept_invitation(raw, name="Sam Assistant", password=PASSWORD)
    sam = world.ctx(email, owner.org_id)
    return owner, leader, ann, bob, cara, sam


def visible_notes(ctx):
    with tenant_session(ctx) as db:
        return db.execute(text("select count(*) from notes")).scalar()


def test_notes_are_private_and_encrypted(world):
    owner, leader, ann, bob, cara, sam = setup(world)
    nid = N.create_note(ann, "Summit keynote", body=BODY, tags=["Skills", "skills", "Financial concepts"])
    d = N.get_note(ann, nid)
    assert d.body == BODY.strip("\n") and d.tags == ["Skills", "Financial concepts"]
    for ctx in (leader, owner, bob, cara, sam):
        with pytest.raises(NotFound):
            N.get_note(ctx, nid)
        assert visible_notes(ctx) == 0
    with system_session("test") as db:
        assert "referrals" not in (db.get(Note, nid).body_enc or "")
    rows = N.list_notes(ann, search="underinsured")
    assert [r.id for r in rows] == [nid] and rows[0].markers == {"key": 1, "action": 1, "question": 1, "stat": 1}
    assert N.list_notes(ann, search="nothing-like-this") == []
    assert [r.id for r in N.list_notes(ann, tag="financial concepts")] == [nid]


def test_team_events_and_sessions(world):
    owner, leader, ann, bob, cara, sam = setup(world)
    with pytest.raises(PermissionDenied):
        N.create_event(ann, "Q4 Summit", for_team=True)  # members can't create team events
    with pytest.raises(PermissionDenied):
        N.create_event(leader, "All hands", whole_org=True)
    eid = N.create_event(leader, "Q4 Summit", for_team=True, location="Charlotte")
    private = N.create_event(ann, "My webinar")
    org_ev = N.create_event(owner, "Annual kickoff", whole_org=True)
    titles = lambda c: {e.title for e in N.list_events(c)}  # noqa: E731
    assert titles(ann) == {"Q4 Summit", "My webinar", "Annual kickoff"}
    assert titles(cara) == {"Q4 Summit", "Annual kickoff"}
    assert titles(bob) == {"Q4 Summit", "Annual kickoff"}
    assert "Q4 Summit" not in titles(sam)  # assistants aren't on the team
    assert titles(leader) == {"Q4 Summit", "Annual kickoff"}
    other = world.org("Other Agency")
    assert titles(other) == set()
    # Attendees add sessions; a note in a session lands under the event.
    sid = N.add_session(ann, eid, "Closing with confidence", speaker="Dana Speaker", starts_at="2pm")
    assert N.get_event(bob, eid).sessions[0].starts_at == "14:00"
    with pytest.raises(PermissionDenied):
        N.update_session(bob, sid, "Renamed")
    N.update_session(leader, sid, "Closing with confidence", "Dana Speaker", "14:30")  # event owner may
    nid = N.create_note(bob, session_id=sid)
    d = N.get_note(bob, nid)
    assert d.title == "Closing with confidence" and d.event_id == eid and d.speaker == "Dana Speaker"
    with pytest.raises(PermissionDenied):
        N.delete_event(ann, eid)
    with pytest.raises(NotFound):
        N.add_session(bob, private, "x")
    N.delete_event(leader, eid)
    assert N.get_note(bob, nid).event_id is None  # the note survives
    assert org_ev


def test_sharing_audiences(world):
    owner, leader, ann, bob, cara, sam = setup(world)
    nid = N.create_note(ann, "Summit keynote", body=BODY, tags=["Financial concepts"])
    sid = N.share(ann, nid, to_leader=True)
    assert any(n.kind == "note_shared" for n in notify.my_notifications(leader))
    assert [r.id for r in N.shared_with_me(leader)] == [sid]
    for ctx in (bob, cara, sam, owner):
        assert N.shared_with_me(ctx) == []
        with pytest.raises(NotFound):
            N.get_shared(ctx, sid)
    s = N.get_shared(leader, sid)
    assert s.body == BODY.strip("\n") and s.how == "you" and s.disclaimer is None and s.audience == "their leader"
    # Team sharing: Ann's downline sees it, not her sibling, not her assistant.
    N.share(ann, nid, to_leader=True, to_team=True)
    assert any(n.kind == "note_shared" for n in notify.my_notifications(cara))
    assert [r.how for r in N.shared_with_me(cara)] == ["team"]
    assert N.shared_with_me(bob) == [] and N.shared_with_me(sam) == []
    assert N.get_shared(cara, sid).disclaimer == N.DISCLAIMER  # financial notes shared widely
    # Specific people: only people in my branch or upline.
    with pytest.raises(PermissionDenied):
        N.share(ann, nid, people=[bob.user_id])
    N.share(ann, nid, people=[sam.user_id])
    assert [r.id for r in N.shared_with_me(sam)] == [sid]
    assert N.shared_with_me(leader) == [] and N.shared_with_me(cara) == []  # audience replaced
    # Members can't forge someone else's share or add recipients to it.
    with tenant_session(bob) as db:
        with pytest.raises(Exception):
            db.execute(text("insert into shared_note_recipients (org_id, shared_id, author_id, user_id, is_leader) "
                            "values (:o, :s, :a, :u, false)"), dict(o=bob.org_id, s=sid, a=bob.user_id, u=bob.user_id))
    with pytest.raises(ValidationError):
        N.share(ann, nid)


def test_snapshot_refresh_comments_and_unshare(world):
    owner, leader, ann, bob, cara, sam = setup(world)
    nid = N.create_note(ann, "Day 1", body="KEY: first point")
    sid = N.share(ann, nid, to_leader=True, to_team=True)
    N.save_note(ann, nid, body="KEY: first point\nKEY: private draft thought")
    assert "private draft" not in N.get_shared(leader, sid).body  # snapshot
    assert N.get_note(ann, nid).share.out_of_date
    N.refresh_share(ann, nid)
    assert "private draft" in N.get_shared(cara, sid).body
    info = N.get_note(ann, nid).share
    assert info.to_leader and info.to_team and not info.out_of_date
    # Comments: recipients comment, the author is notified; the author's reply notifies commenters.
    N.comment(leader, sid, "Great takeaways — let's role-play the referral ask Tuesday")
    assert any(n.kind == "note_comment" for n in notify.my_notifications(ann))
    N.comment(ann, sid, "Tuesday works")
    assert any(n.kind == "note_comment" for n in notify.my_notifications(leader))
    assert [c.body for c in N.get_shared(cara, sid).thread] == [
        "Great takeaways — let's role-play the referral ask Tuesday", "Tuesday works"]
    with pytest.raises(NotFound):
        N.comment(bob, sid, "hi")
    N.unshare(ann, nid)
    with pytest.raises(NotFound):
        N.get_shared(leader, sid)
    assert N.get_note(ann, nid).share is None


def test_takeaways_to_goals_and_tasks(world):
    owner, leader, ann, bob, cara, sam = setup(world)
    eid = N.create_event(ann, "Summit")
    nid = N.create_note(ann, "Keynote", event_id=eid, body=BODY)
    N.create_note(ann, "Elsewhere", body="ACTION: unrelated")
    items = N.takeaways(ann, eid)
    assert [(t.kind, t.text) for t in items] == [
        ("key", "Ask for referrals at every policy review"), ("action", "Call my top 10 clients this week"),
        ("question", "How do we handle clients who already have term?"), ("stat", "60% of families are underinsured")]
    act = items[1]
    gid = N.make_goal(ann, nid, act.key)
    g = [x for x in goals.list_goals(ann) if x.id == gid][0]
    assert g.title == "Call my top 10 clients this week" and g.status == "proposed"  # goes to the leader
    assert N.takeaways(ann, eid)[1].made == "goal"
    with pytest.raises(ValidationError):
        N.make_task(ann, nid, act.key)
    with pytest.raises(NotFound):
        N.make_task(ann, nid, items[0].key)  # only action items
    nid2 = N.create_note(ann, "Breakout", event_id=eid, body="TODO: Send recap to my team")
    t = [x for x in N.takeaways(ann, eid) if x.note_id == nid2][0]
    tid = N.make_task(ann, nid2, t.key, assignee_id=sam.user_id)
    assert T.get(sam, tid).title == "Send recap to my team"
    with pytest.raises(NotFound):
        N.make_goal(bob, nid, act.key)


def _jpeg_with_gps() -> bytes:
    im = Image.new("RGB", (3000, 1500), "navy")
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (35.0, 47.0, 0.0)}  # GPS info
    exif[0x010F] = "PhoneMaker"
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif)
    return buf.getvalue() + b"TRAILING-PAYLOAD"


def test_slide_photos_are_cleaned_and_shared(world, tmp_path):
    owner, leader, ann, bob, cara, sam = setup(world)
    nid = N.create_note(ann, "Slides")
    src = tmp_path / "slide.jpg"
    src.write_bytes(_jpeg_with_gps())
    pid = N.add_photo(ann, nid, src, "IMG_0042.jpg", caption="Pricing slide")
    d = N.get_note(ann, nid)
    assert [p.caption for p in d.photos] == ["Pricing slide"] and d.photos[0].url.startswith("/files/")
    with system_session("test") as db:
        f = db.get(StoredFile, d.photos[0].file_id)
        from teampath import storage

        data = storage.absolute(f.storage_path).read_bytes()
    assert b"TRAILING-PAYLOAD" not in data and b"PhoneMaker" not in data
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) == 2000 and not im.getexif()
    bad = tmp_path / "x.png"
    bad.write_bytes(b"\x89PNG not really")
    with pytest.raises(ValidationError):
        N.add_photo(ann, nid, bad, "x.png")
    with pytest.raises(NotFound):
        N.add_photo(bob, nid, tmp_path / "missing.jpg", "y.jpg")
    sid = N.share(ann, nid, to_leader=True)
    assert len(N.get_shared(leader, sid).photos) == 1
    # Removing the photo keeps the shared copy's image until the share is updated or removed.
    N.remove_photo(ann, pid)
    with system_session("test") as db:
        assert db.get(StoredFile, d.photos[0].file_id) is not None
    N.unshare(ann, nid)
    with system_session("test") as db:
        assert db.get(StoredFile, d.photos[0].file_id) is None


def test_delete_note_removes_shared_copy(world):
    owner, leader, ann, bob, cara, sam = setup(world)
    nid = N.create_note(ann, "Temp", body="KEY: x")
    sid = N.share(ann, nid, to_leader=True)
    N.delete_note(ann, nid)
    with system_session("test") as db:
        assert db.get(SharedNote, sid) is None
    with pytest.raises(NotFound):
        N.get_note(ann, nid)
