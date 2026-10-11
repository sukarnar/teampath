"""UI tests for admin and leader tools: org settings, branding, people management, map layout,
sign-offs, opportunities, notifications and live updates."""

import asyncio
import io

from PIL import Image

from teampath.services import orgs, paths, qa
from teampath.worker.main import drain
from test_ui_flows import no_mfa, run, sign_in


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 300), (20, 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


def test_org_settings_branding_and_people(world):
    owner = world.org("Admin Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    other = world.member(owner, "Otto Other", role="leader")

    async def go():
        o = await sign_in(owner.user_email)
        try:
            await o.goto("/org")
            await o.fill("Website", "https://admin-agency.example.com")
            await o.click("Save")
            assert o.has_text("Saved.")
            await o.goto("/org?tab=brand")
            await o.fill("Brand color", "#0F6E56")
            await o.fill("Tagline", "Protecting families since 1999")
            await o.click("Save branding")
            assert o.page.theme.color_scheme_seed == "#0F6E56"
            o.picked_files = [("logo.png", _png())]
            await o.click("Upload logo")
            await o.settle(quiet=0.6)
            drain()
            await o.goto("/org?tab=brand")
            assert o.state.logo_url, "logo should be available after processing"
            await o.goto("/org?tab=settings")
            await o.click("Save settings")
            await o.goto("/org?tab=activity")
            await o.fill("New activity", "Follow-up calls")
            await o.click("Add")
            assert o.find(lambda x: getattr(x, "value", None) == "Follow-up calls")
            await o.goto("/org?tab=audit")
            assert o.has_text("org.branding_updated")
            # People: move the agent to another leader, then release them.
            await o.goto("/people")
            menu = o.find(lambda x: x.__class__.__name__ == "PopupMenuButton" and getattr(x, "tooltip", "") == "Manage"
                          and any("Release" in (getattr(i.content, "value", "") or "") for i in x.items))
            assert menu
            await o.goto("/people?invite=1")
            await o.settle(quiet=0.5)
            assert o.dialogs(), "invite dialog should open from the deep link"
        finally:
            await o.close()
        orgs.move_member(owner, agent.membership_id, other.membership_id)
        assert any(p.user_id == agent.user_id for p in orgs.list_people(world.ctx(other.user_email, owner.org_id)))

    run(go())


def test_leader_tools(world):
    owner = world.org("Leader Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    world.member(agent, "Rex Recruit", parent=agent)
    pid = paths.create_path(owner, "Licensing")
    paths.save_step(owner, pid, title="Pass the exam", requires_signoff=True)
    paths.publish(owner, pid)

    async def go():
        c = await sign_in(leader.user_email)
        try:
            # Assign a path from the person page.
            await c.goto(f"/people/{agent.user_id}")
            await c.click("Assign a path")
            await c.settle(quiet=0.5)
            await c.click("Assign", in_dialog=True)
            assert paths.person_enrollments(leader, agent.user_id)
            # Map: arrange mode, drag, save.
            await c.goto("/team?view=arrange")
            gd = c.find(lambda x: x.__class__.__name__ == "GestureDetector" and getattr(x, "data", None) == str(agent.user_id))
            assert gd
            await c._send(3, {"target": gd._i, "name": "pan_update", "data": {
                "l": {"x": 0, "y": 0}, "g": {"x": 0, "y": 0}, "ld": {"x": 40, "y": 25}, "gd": {"x": 40, "y": 25}}})
            await c.settle()
            await c.click("Save layout")
            assert c.has_text("Layout saved")
            # Zoom into the agent's team.
            await c.goto(f"/team?sel={agent.user_id}")
            await c.click("Zoom into team")
            assert c.has_text("Ann Agent's team")
            # Share an opportunity.
            await c.goto("/opportunities")
            await c.click("Share an opportunity")
            await c.fill("Teammate", str(agent.user_id), in_dialog=True)
            await c.fill("Short label (no client names or details)", "Expo referral", in_dialog=True)
            await c.click("Share", in_dialog=True)
            assert c.has_text("Expo referral")
        finally:
            await c.close()

        # Agent completes the sign-off step; leader sees it live and signs off.
        step = paths.my_enrollments(agent)[0].steps[0]
        paths.complete_step(agent, step.id, "Passed!")
        c = await sign_in(leader.user_email)
        try:
            await c.goto("/team-progress")
            assert c.has_text("Pass the exam")
            await c.click("Sign off")
            assert c.has_text("Signed off.")
            # Notifications dialog.
            await c.click("Notifications")
            assert c.dialogs()
        finally:
            await c.close()

    run(go())


def test_live_update_reaches_open_session(world):
    owner = world.org("Live Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)

    async def go():
        c = await sign_in(leader.user_email)
        try:
            await c.goto("/questions")
            assert c.has_text("No open questions")
            await asyncio.to_thread(qa.ask, agent, "Live question?", "Does this show up?")
            await asyncio.sleep(2.0)
            await c.settle()
            assert c.has_text("Live question?"), c.texts()[-800:]
            badge = c.find(lambda x: getattr(x, "tooltip", None) == "Notifications")
            assert badge.badge is not None
        finally:
            await c.close()

    run(go())


def test_idle_timeout_signs_out(world, monkeypatch):
    owner = world.org("Idle Agency")
    no_mfa(owner)

    async def go():
        c = await sign_in(owner.user_email)
        try:
            c.state.last_seen -= 60 * 60
            await c.goto("/learn")
            assert c.route == "/login"
            assert c.has_text("inactivity")
        finally:
            await c.close()

    run(go())


def test_leader_sees_and_approves_proposed_goal(world):
    owner = world.org("Goal Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)

    async def go():
        a = await sign_in(agent.user_email)
        try:
            await a.goto("/goals")
            await a.click("Propose a goal")
            await a.fill("Goal", "Get appointed with 3 carriers", in_dialog=True)
            await a.click("Propose", in_dialog=True)
            assert a.has_text("Waiting for approval")
        finally:
            await a.close()
        c = await sign_in(leader.user_email)
        try:
            tile = c.find(lambda x: getattr(x, "on_click", None) is not None
                          and "Goals to approve" in " ".join(t.value for t in _texts(x)))
            assert tile and any(t.value == "1" for t in _texts(tile)), c.texts()[:1200]
            await c.goto("/goals")  # opens on the approval tab when something is waiting
            assert c.has_text("Get appointed with 3 carriers") and c.has_text("Ann Agent")
            await c.goto("/team-progress")
            assert c.has_text("Goals waiting for approval")
            await c.goto("/goals?tab=approve")
            await c.click("Approve")
            assert c.has_text("No goals waiting for your approval")
        finally:
            await c.close()
        from teampath.services import goals

        assert goals.list_goals(agent)[0].status == "active"

    run(go())


def _texts(control):
    import flet as ft
    from ui_client import _walk

    return [x for x in _walk([control]) if isinstance(x, ft.Text) and isinstance(x.value, str)]
