"""End-to-end: an agent invites an assistant, delegates a prospect task; the assistant does it."""

import flet as ft

from conftest import PASSWORD
from teampath.services import notify
from teampath.services import prospects as P
from teampath.services import tasks as T
from test_ui_flows import no_mfa, run, sign_in
from test_ui_prospects import chip
from ui_client import UIClient


def test_delegate_to_assistant(world):
    owner = world.org("Delegate Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    pid = P.create(agent, name="Maria Lopez", phone="9195550142", details="Private note: renewing in March")

    async def go():
        a = await sign_in(agent.user_email)
        try:
            await a.goto("/tasks/assistants")
            await a.click("Invite an assistant")
            await a.fill("Assistant's email", "sam.asst@example.com", in_dialog=True)
            await a.click("Send invitation", in_dialog=True)
            link = next(t for t in a.texts().splitlines() if "/invite?token=" in t)
        finally:
            await a.close()

        s = await UIClient().connect(f"/invite?token={link.split('token=')[1].strip()}")
        try:
            assert s.has_text("invited as assistant")
            await s.fill("Your name", "Sam Assistant")
            await s.fill("Choose a password", PASSWORD)
            await s.click("Join Delegate Agency")
            await s.settle()
            assert s.route == "/"
            rail = s.find(lambda x: isinstance(x, ft.NavigationRail))
            assert [d.label for d in rail.destinations] == ["Today", "Tasks", "Learn", "Notes", "Library", "Questions", "People"]
            assert s.has_text("Nothing assigned to you right now.") and not s.has_text("Today's activity")
            await s.goto("/prospects")
            assert s.route == "/tasks"  # assistants have no prospects of their own
        finally:
            await s.close()

        a = await sign_in(agent.user_email)
        try:
            await a.goto(f"/prospects/{pid}")
            item = a.find(lambda x: isinstance(x, ft.PopupMenuItem)
                          and getattr(x.content, "value", "") == "Delegate a task about them")
            await a.fire(item)
            await a.settle(quiet=0.5)
            await a.fill("Task", "Confirm Tuesday's appointment with Maria", in_dialog=True)
            await a.fill("Instructions (optional)", "Ask if her husband will join", in_dialog=True)
            await a.segment("0", in_dialog=True)
            await a.click("Delegate", in_dialog=True)
            await a.settle()
            assert a.route.startswith("/tasks/"), a.route
            task_route = a.route
            assert a.has_text("Delegated by Ann Agent to Sam Assistant")
        finally:
            await a.close()

        lead = await sign_in(leader.user_email)
        try:
            await lead.goto(task_route)
            assert lead.has_text("This page isn't available")
        finally:
            await lead.close()

        s = await sign_in("sam.asst@example.com")
        try:
            assert s.has_text("Confirm Tuesday's appointment with Maria") and s.has_text("1 due today")
            await s.goto(task_route)
            assert s.has_text("(919) 555-0142") and s.has_text("Ask if her husband will join")
            assert not s.has_text("renewing in March")  # the agent's private notes stay private
            assert s.find(lambda x: isinstance(x, ft.Chip) and getattr(x.label, "value", "") == "Became a client") is None
            await s.fire(chip(s, "Appointment set"))
            await s.click("Save")
            assert s.has_text("by Sam Assistant"), s.texts()[-800:]
            await s.fill("Add a comment", "Confirmed - both attending, 7pm")
            await s.click("Send")
            await s.click("Mark done")
            await s.fill("Note for Ann Agent (optional)", "All set", in_dialog=True)
            await s.click("Done", in_dialog=True)
            assert s.has_text("Ann Agent has been notified")
            assert not s.has_text("(919) 555-0142")  # access ended with the task
        finally:
            await s.close()

        kinds = {n.kind for n in notify.my_notifications(agent)}
        assert {"task_comment", "task_done"} <= kinds
        d = P.get(agent, pid)
        assert d.stage == "appointment" and d.touches[0].by_name == "Sam Assistant"
        assert T.summary(agent).delegated_done_today == 1

    run(go())
