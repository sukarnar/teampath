"""End-to-end UI tests: real server, real Flet sessions, driven through the websocket protocol."""

import asyncio
import io

import fitz
import pyotp

from conftest import PASSWORD
from teampath.services import activity, orgs, paths, qa, updates
from ui_client import UIClient


def run(coro):
    return asyncio.run(coro)


async def sign_in(email, route="/login", width=1280):
    c = await UIClient(width=width).connect(route)
    await c.fill("Email", email)
    await c.fill("Password", PASSWORD)
    await c.click("Sign in")
    await c.settle()
    return c


def no_mfa(owner):
    orgs.update_settings(owner, mfa_required_roles=[])


def test_owner_must_set_up_mfa_then_reaches_today(world):
    owner = world.org("MFA Agency")

    async def go():
        c = await sign_in(owner.user_email)
        try:
            assert c.route == "/security", c.route
            assert c.has_text("requires two-step verification")
            await c.click("Set up two-step verification")
            key = c.find(lambda x: getattr(x, "font_family", None) == "monospace")
            secret = key.value.replace(" ", "")
            await c.fill("6-digit code", "000000", in_dialog=True)
            await c.click("Turn on", in_dialog=True)
            assert c.has_text("didn't match")
            await c.fill("6-digit code", pyotp.TOTP(secret).now(), in_dialog=True)
            await c.click("Turn on", in_dialog=True)
            await c.settle()
            assert c.route == "/", c.route
            assert c.has_text("Today's activity")
        finally:
            await c.close()
        # Next sign-in asks for the code.
        c = await sign_in(owner.user_email)
        try:
            assert c.route == "/mfa"
            await c.fill("6-digit code", pyotp.TOTP(secret).now())
            await c.click("Verify")
            await c.settle()
            assert c.route == "/"
        finally:
            await c.close()

    run(go())


ALL_ROUTES = ["/", "/learn", "/goals", "/updates", "/updates?tab=team", "/questions", "/questions?tab=mine", "/team",
              "/team?view=list", "/team?view=arrange", "/team?size=activity", "/team-progress", "/opportunities",
              "/people", "/library", "/paths", "/org", "/org?tab=brand", "/org?tab=settings", "/org?tab=activity",
              "/org?tab=audit", "/security", "/orgs", "/prospects", "/prospects?tab=lists", "/prospects?tab=pipeline",
              "/prospects?tab=all", "/prospects/list/due", "/tasks", "/tasks?tab=done", "/tasks?tab=delegated",
              "/tasks/assistants"]


def test_every_page_renders_for_each_role(world):
    owner = world.org("Render Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)
    pid = paths.create_path(owner, "Onboarding")
    paths.save_step(owner, pid, title="Read the handbook", instructions_md="**Welcome**")
    paths.publish(owner, pid)
    paths.enroll(owner, pid, [agent.user_id])
    activity.log(agent, activity.tiles(agent)[0].type_id, 3)
    tid = qa.ask(agent, "Which carrier?", "Help")
    uid = updates.send_update(agent, wins="Good week")
    step_id = paths.my_enrollments(agent)[0].steps[0].id

    admin_only = {r for r in ALL_ROUTES if r.startswith("/org?") or r == "/org"}

    async def visit(email, extra, denied):
        for width in (1280, 420):  # desktop and phone layouts
            c = await sign_in(email, width=width)
            try:
                for r in ALL_ROUTES + extra:
                    await c.goto(r)
                    c.assert_ok()
                    blocked = c.has_text("This page isn't available")
                    assert blocked == (r in denied), (email, r, c.texts()[:1500])
            finally:
                await c.close()

    run(visit(owner.user_email, [f"/people/{agent.user_id}", f"/questions/{tid}", f"/updates/{uid}",
                                 f"/paths/{pid}", f"/learn/step/{step_id}", f"/team?root={leader.user_id}"], set()))
    run(visit(leader.user_email, [f"/people/{agent.user_id}", f"/questions/{tid}", f"/updates/{uid}",
                                  f"/team?sel={agent.user_id}"], admin_only))
    run(visit(agent.user_email, [f"/learn/step/{step_id}", f"/questions/{tid}", f"/updates/{uid}"],
              admin_only | {"/paths"}))


def test_agent_cannot_open_admin_pages(world):
    owner = world.org("Guard Agency")
    no_mfa(owner)
    agent = world.member(owner, "Ann Agent")

    async def go():
        c = await sign_in(agent.user_email)
        try:
            for r in ("/org", "/paths", f"/people/{owner.user_id}"):
                await c.goto(r)
                assert c.has_text("This page isn't available") or c.has_text("above you"), (r, c.texts()[:800])
            await c.goto("/platform")
            assert c.route == "/"
        finally:
            await c.close()

    run(go())


def test_full_team_flow_through_the_ui(world):
    owner = world.org("Flow Agency")
    no_mfa(owner)

    async def go():
        o = await sign_in(owner.user_email)
        try:
            # Author a path.
            await o.goto("/paths")
            await o.click("New path")
            await o.fill("Path title", "First 30 days", in_dialog=True)
            await o.click("Create", in_dialog=True)
            await o.settle()
            assert o.route.startswith("/paths/"), o.route
            await o.click("Add step")
            await o.fill("Step title", "Get licensed", in_dialog=True)
            sw = o.find(lambda x: getattr(x, "label", None) == "Leader must sign off", in_dialog=True)
            await o.set_value(sw, True)
            await o.click("Save", in_dialog=True)
            assert o.has_text("Get licensed")
            await o.click("Publish")
            assert o.has_text("Published version 1"), o.texts()[-1500:]
            # Invite a leader (no SMTP in tests, so the link is shown).
            await o.goto("/people")
            await o.click("Invite")
            await o.fill("Email", "lead.ui@example.com", in_dialog=True)
            await o.fill("Role", "leader", in_dialog=True)
            await o.click("Send invitation", in_dialog=True)
            link = next(t for t in o.texts().splitlines() if "/invite?token=" in t)
        finally:
            await o.close()

        token = link.split("token=")[1].strip()
        lead = await UIClient().connect(f"/invite?token={token}")
        try:
            assert lead.has_text("Join Flow Agency")
            await lead.fill("Your name", "Lena Lead")
            await lead.fill("Choose a password", PASSWORD)
            await lead.click("Join Flow Agency")
            await lead.settle()
            assert lead.route == "/", (lead.route, lead.texts()[:500])
            # The leader recruits an agent.
            await lead.goto("/people")
            await lead.click("Invite")
            await lead.fill("Email", "agent.ui@example.com", in_dialog=True)
            await lead.click("Send invitation", in_dialog=True)
            link2 = next(t for t in lead.texts().splitlines() if "/invite?token=" in t)
        finally:
            await lead.close()

        ag = await UIClient(width=420).connect(f"/invite?token={link2.split('token=')[1].strip()}")
        try:
            await ag.fill("Your name", "Andy Agent")
            await ag.fill("Choose a password", PASSWORD)
            await ag.click("Join Flow Agency")
            await ag.settle()
            assert ag.route == "/"
            # Log activity from the phone layout.
            await ag.click("+1")
            await ag.click("+1")
            assert ag.find(lambda x: getattr(x, "value", None) == "2" and getattr(x, "size", None) == 30)
            # Propose a goal.
            await ag.goto("/goals")
            await ag.click("Propose a goal")
            await ag.fill("Goal", "Get appointed with 3 carriers", in_dialog=True)
            await ag.click("Propose", in_dialog=True)
            assert ag.has_text("Waiting for approval")
            # Ask a question.
            await ag.goto("/questions")
            await ag.click("Ask a question")
            await ag.fill("Question (short)", "Which carrier first?", in_dialog=True)
            await ag.fill("Details", "I'm new.", in_dialog=True)
            await ag.click("Ask", in_dialog=True)
            await ag.settle()
            assert ag.route.startswith("/questions/")
            # Send the weekly update.
            await ag.goto("/updates")
            await ag.fill("Wins this week", "Two appointments set")
            await ag.click("Send update")
            assert ag.has_text("Awaiting feedback")
        finally:
            await ag.close()

        # Leader: map, focus, answer, feedback, approve goal.
        lead = await sign_in("lead.ui@example.com")
        try:
            await lead.goto("/team")
            bubble = lead.find(lambda x: "Andy Agent" in (getattr(x, "tooltip", None) or "")
                               and getattr(x, "on_click", None) is not None)
            assert bubble, lead.texts()[:1000]
            await lead.fire(bubble)
            await lead.settle()
            assert lead.has_text("Your private notes")
            await lead.click("Focus")
            await lead.goto("/")
            assert lead.has_text("Andy Agent") and lead.has_text("Today's focus")
            await lead.goto("/questions")
            await lead.click("Which carrier first?")
            await lead.fill("Write a reply", "Start with our two appointed carriers.")
            await lead.click("Reply")
            assert lead.has_text("Answered")
            await lead.goto("/updates?tab=team")
            await lead.click("Andy Agent")
            await lead.segment("coaching")
            await lead.fill("Your feedback", "Great start — let's role-play objections.")
            await lead.click("Send")
            assert lead.has_text("role-play objections")
            people = [m for m in orgs.list_people(lead_ctx(world))]
            andy = next(p for p in people if p.name == "Andy Agent")
            await lead.goto(f"/people/{andy.user_id}")
            await lead.click("Approve")
            assert lead.has_text("Active")
        finally:
            await lead.close()

    run(go())


def lead_ctx(world):
    return world.ctx("lead.ui@example.com")


def _pdf_bytes(pages=2) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 72), f"Carrier guide page {i + 1}")
    buf = io.BytesIO(doc.tobytes())
    return buf.getvalue()


def test_upload_preview_and_watermark(world):
    owner = world.org("Upload Agency")
    no_mfa(owner)
    agent = world.member(owner, "Ann Agent")

    async def go():
        o = await sign_in(owner.user_email)
        try:
            await o.goto("/library")
            await o.click("Add")
            o.picked_files = [("Carrier guide.pdf", _pdf_bytes(12))]
            await o.click("Choose file", in_dialog=True)
            await o.settle(quiet=0.6)
            assert o.has_text("Carrier guide.pdf"), o.texts()[:1500]
            sw = o.find(lambda x: getattr(x, "label", None) == "Watermark pages with the viewer's name", in_dialog=True)
            await o.set_value(sw, True)
            await o.click("Add", in_dialog=True)
            await o.settle()
            assert o.route.startswith("/content/"), o.route
            content_route = o.route
            # The PDF is viewable right away — no background worker involved.
            assert not o.has_text("Preparing") and not o.has_text("Checking the file"), o.texts()[-800:]
            assert o.has_text("12 pages") and o.button("Open PDF")
            await o.goto("/library")
            assert o.has_text("Carrier guide") and o.has_text("preview ready"), o.texts()[-800:]
        finally:
            await o.close()
        from teampath.db import system_session
        from sqlalchemy import text as sql

        with system_session("test") as db:
            assert db.execute(sql("select count(*) from jobs where status = 'queued'")).scalar() == 0
        a = await sign_in(agent.user_email)
        try:
            rail = a.find(lambda x: x.__class__.__name__ == "NavigationRail")
            assert "Library" in [d.label for d in rail.destinations]  # every role has the library in the menu
            await a.goto(content_route)

            def images():
                return [x for x in a.controls()
                        if x.__class__.__name__ == "Image" and "/files/" in str(getattr(x, "src", ""))]

            assert len(images()) == 5, a.texts()[:1500]
            await a.click("Show more pages")
            assert len(images()) == 12
            assert not a.button("Show more pages").visible
            imgs = images()
            import httpx

            for img in (imgs[0], imgs[11]):  # rendered on first request
                r = httpx.get(a.base + img.src)
                assert r.status_code == 200 and r.headers["content-type"] == "image/png"
            # The signature is bound to the viewer.
            forged = imgs[0].src.replace(str(agent.user_id), str(owner.user_id))
            assert httpx.get(a.base + forged).status_code == 403
        finally:
            await a.close()

    run(go())


def test_session_token_remembered_and_revoked(world):
    owner = world.org("Remember Agency")
    no_mfa(owner)

    async def go():
        c = await UIClient().connect("/login")
        await c.fill("Email", owner.user_email)
        await c.fill("Password", PASSWORD)
        cb = c.find(lambda x: getattr(x, "label", None) == "Keep me signed in on this device")
        await c.set_value(cb, True)
        await c.click("Sign in")
        await c.settle()
        prefs = dict(c.prefs)
        await c.close()
        assert prefs.get("tp.session")
        c2 = UIClient()
        c2.prefs = prefs
        await c2.connect("/learn")
        try:
            assert c2.route == "/learn", c2.route
            await c2.goto("/security")
            await c2.click("Sign out all others")
            await c2.click("Sign out others", in_dialog=True)
        finally:
            await c2.close()

    run(go())
