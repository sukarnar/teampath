"""End-to-end: a leader creates a team event; an agent takes notes in a session, marks takeaways,
turns an action item into a task, shares with the leader; the leader comments."""

import flet as ft

from teampath.services import notes as N
from teampath.services import notify
from test_ui_flows import no_mfa, run, sign_in


def test_notes_flow(world):
    owner = world.org("Notes Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    sibling = world.member(leader, "Bob Agent", parent=leader)

    async def go():
        lead = await sign_in(leader.user_email)
        try:
            await lead.goto("/notes?tab=events")
            await lead.click("New event")
            await lead.fill("Event name", "Q4 Summit", in_dialog=True)
            team = lead.find(lambda x: isinstance(x, ft.Switch) and "Team event" in (x.label or ""), in_dialog=True)
            await lead.set_value(team, True)
            await lead.click("Create", in_dialog=True)
            assert lead.route.startswith("/notes/events/"), lead.route
            event_route = lead.route
            await lead.click("Add session")
            await lead.fill("Session or talk", "Closing with confidence", in_dialog=True)
            await lead.fill("Speaker (optional)", "Dana Speaker", in_dialog=True)
            await lead.click("Add", in_dialog=True)
            assert lead.has_text("Closing with confidence")
        finally:
            await lead.close()

        a = await sign_in(agent.user_email)
        try:
            await a.goto("/notes?tab=events")
            assert a.has_text("Q4 Summit") and a.has_text("Team event · Lee Leader")
            await a.goto(event_route)
            await a.click("Take notes")
            assert a.route.startswith("/notes/") and "events" not in a.route, a.route
            note_route = a.route
            assert a.field("Title").value == "Closing with confidence"
            body = a.find(lambda x: isinstance(x, ft.TextField) and x.multiline and x.label is None)
            await a.set_value(body, "Great opener")
            await a.click("Key point")
            assert body.value == "Great opener\nKEY: "
            await a.set_value(body, "Great opener\nKEY: Ask for referrals at every review\n"
                                    "ACTION: Call my top 10 clients\nSTAT: 60% are underinsured")
            await a.fire(a.find(lambda x: isinstance(x, ft.Chip) and getattr(x.label, "value", "") == "Skills"))
            await a.click("Done")
            assert a.route == event_route
            assert a.has_text("Your note: Closing with confidence")
            nid = N.list_notes(agent)[0].id
            d = N.get_note(agent, nid)
            assert "ACTION: Call my top 10 clients" in d.body and d.tags == ["Skills"] and d.speaker == "Dana Speaker"

            # Takeaways → task
            await a.click("Takeaways")
            assert a.has_text("Key points (1)") and a.has_text("Stats and quotes — verify before using (1)")
            await a.click("Make task")
            await a.settle()
            assert a.has_text("Became a task") and not a.find(lambda x: getattr(x, "text", None) == "Make task")

            # Share with the leader
            await a.goto(note_route)
            await a.click("Share…")
            await a.settle(quiet=0.5)
            await a.click("Share", in_dialog=True)
            assert a.has_text("With your leader")
        finally:
            await a.close()
        assert any(n.kind == "note_shared" for n in notify.my_notifications(leader))

        lead = await sign_in(leader.user_email)
        try:
            await lead.goto("/notes?tab=shared")
            assert lead.has_text("Q4 Summit") and lead.has_text("Closing with confidence — Ann Agent")
            await lead.click("Closing with confidence — Ann Agent")
            assert lead.route.startswith("/notes/shared/")
            assert lead.has_text("Ask for referrals at every review")
            await lead.fill("Add a comment or question", "Let's role-play this Tuesday")
            await lead.click("Send")
            assert lead.has_text("Let's role-play this Tuesday")
            await lead.goto(note_route)  # the private note itself stays private
            assert lead.has_text("This page isn't available")
        finally:
            await lead.close()
        assert any(n.kind == "note_comment" for n in notify.my_notifications(agent))

        b = await sign_in(sibling.user_email)
        try:
            await b.goto("/notes?tab=shared")
            assert not b.has_text("Ann Agent") and b.has_text("Nothing shared with you yet")
        finally:
            await b.close()

    run(go())
