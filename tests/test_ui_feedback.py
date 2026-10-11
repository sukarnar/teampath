"""Leader feedback on a weekly update must reach the team member — live, on Today, and on the update."""

import asyncio

from teampath.services import notify, updates
from test_ui_flows import no_mfa, run, sign_in


def test_feedback_reaches_member(world):
    owner = world.org("Feedback Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    uid = updates.send_update(agent, wins="Two appointments", help_wanted="Objections")
    msg = "Great start - let's role-play objections Tuesday"

    async def go():
        a = await sign_in(agent.user_email)
        await a.goto(f"/updates/{uid}")  # the member has the update open while the leader replies
        assert a.has_text("No feedback yet.")
        c = await sign_in(leader.user_email)
        try:
            await c.goto(f"/updates/{uid}")
            await c.segment("coaching")
            await c.fill("Your feedback", msg)
            await c.click("Send")
            assert c.has_text("Feedback sent to Ann Agent"), c.texts()[-600:]
            assert c.has_text(msg)
            # The member's open page updates by itself.
            for _ in range(20):
                await asyncio.sleep(0.25)
                if a.has_text(msg):
                    break
            await a.settle()
            assert a.has_text(msg) and not a.has_text("No feedback yet."), a.texts()[-600:]
            badge = a.find(lambda x: getattr(x, "tooltip", None) == "Notifications")
            assert badge.badge is not None
        finally:
            await c.close()
            await a.close()
        # Today shows new feedback until the member opens it.
        updates.add_feedback(leader, uid, "Also: nice job on the follow-ups", "kudos")
        assert [f.kind for f in updates.unseen_feedback(agent)] == ["kudos"]  # the first one was seen live
        a = await sign_in(agent.user_email)
        try:
            assert a.has_text("New feedback for you") and a.has_text("nice job on the follow-ups"), a.texts()[:1500]
            import flet as ft

            tile = a.find(lambda x: isinstance(x, ft.ListTile) and "follow-ups" in str(getattr(x.subtitle, "value", "")))
            await a.fire(tile)
            assert a.route == f"/updates/{uid}"
            await a.goto("/")
            assert not a.has_text("New feedback for you")
            # The member replies; the leader is notified.
            await a.goto(f"/updates/{uid}")
            await a.fill("Reply", "Tuesday works, thank you!")
            await a.click("Send")
            assert a.has_text("Reply sent.")
        finally:
            await a.close()
        assert any(n.kind == "update_reply" for n in notify.my_notifications(leader))
        assert any(n.kind == "feedback" for n in notify.my_notifications(agent))

    run(go())
