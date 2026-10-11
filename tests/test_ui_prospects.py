"""End-to-end UI tests for prospects: add, log & next step, focus session, lists, import/export, privacy."""

import flet as ft
import httpx

from teampath.services import prospects as P
from test_ui_flows import no_mfa, run, sign_in


def chip(c, label):
    x = c.find(lambda x: isinstance(x, ft.Chip) and getattr(x.label, "value", "") == label)
    assert x, f"no chip {label}: " + c.texts()[-800:]
    return x


def test_agent_prospect_flow(world):
    owner = world.org("Prospect Agency")
    no_mfa(owner)
    leader = world.member(owner, "Lee Leader", role="leader")
    agent = world.member(leader, "Ann Agent", parent=leader)

    async def go():
        a = await sign_in(agent.user_email)
        try:
            rail = a.find(lambda x: isinstance(x, ft.NavigationRail))
            assert [d.label for d in rail.destinations][:2] == ["Today", "Prospects"]
            await a.goto("/prospects")
            assert a.has_text("Add your first prospect")
            # Add a prospect.
            await a.click("Add prospect")
            await a.fill("Name", "Maria Lopez", in_dialog=True)
            await a.fill("Phone", "(919) 555-0142", in_dialog=True)
            await a.fill("Tags (comma separated)", "expo", in_dialog=True)
            await a.click("Add", in_dialog=True)
            assert a.route.startswith("/prospects/"), a.route
            detail_route = a.route
            assert a.has_text("(919) 555-0142") and a.has_text("First contact · today")
            def call_button():
                return a.find(lambda x: str(getattr(x, "url", "") or "").startswith("tel:"))

            assert call_button().url == "tel:9195550142"
            # Log: a call that set an appointment; must pick an outcome first.
            await a.click("Save & set next step")
            assert a.has_text("Choose what happened.")
            await a.fire(chip(a, "Appointment set"))
            assert a.field("Next step").value == "Appointment"
            await a.click("Save & set next step")
            assert a.has_text("Counted: Dials, Contacts, Appointments set"), a.texts()[-600:]
            assert a.find(lambda x: isinstance(x, ft.Dropdown) and x.label == "Stage").value == "appointment"
            assert a.has_text("Call — Appointment set")
            # Do-not-call disables the call button.
            cb = a.find(lambda x: isinstance(x, ft.Checkbox) and x.label == "Do not call")
            await a.set_value(cb, True)
            assert call_button().disabled
            # Duplicate by phone opens the existing prospect.
            await a.goto("/prospects")
            await a.click("Add prospect")
            await a.fill("Name", "M Lopez", in_dialog=True)
            await a.fill("Phone", "919.555.0142", in_dialog=True)
            await a.click("Add", in_dialog=True)
            assert a.route == detail_route
            # Leader can't open it.
            leader_view = await sign_in(leader.user_email)
            try:
                await leader_view.goto(detail_route)
                assert leader_view.has_text("This page isn't available")
                await leader_view.goto("/prospects")
                assert not leader_view.has_text("Maria")
            finally:
                await leader_view.close()
        finally:
            await a.close()

    run(go())


def test_focus_session_from_today(world):
    owner = world.org("Focus Agency")
    no_mfa(owner)
    agent = world.member(owner, "Ann Agent")
    for n in ("Al Adams", "Bea Brown", "Cy Cole"):
        P.create(agent, name=n, phone=f"919555{abs(hash(n)) % 10000:04d}")

    async def go():
        a = await sign_in(agent.user_email)
        try:
            assert a.has_text("Prospect follow-ups") and a.has_text("3 due today")
            await a.click("Start follow-ups")
            assert a.route.startswith("/prospects/focus/"), a.route
            assert a.has_text("0 of 3 done") and a.has_text("Al Adams")
            await a.fire(chip(a, "No answer"))
            await a.click("Save & next")
            assert a.has_text("1 of 3 done") and a.has_text("Bea Brown"), a.texts()[-600:]
            await a.click("Skip for now")
            assert a.has_text("Cy Cole")
            await a.segment("note")
            await a.fire(chip(a, "Not interested"))
            assert a.has_text("This closes the prospect")
            await a.click("Save & next")
            assert a.has_text("List finished") and a.has_text("No answer") and a.has_text("Skipped")
            await a.goto("/")
            assert a.has_text("1 due today")  # Bea was skipped, so she's still due; the others moved on
            await a.goto("/prospects?tab=lists")
            assert a.has_text("Finished")
        finally:
            await a.close()

    run(go())


def test_lists_filters_import_export(world):
    owner = world.org("List Agency")
    no_mfa(owner)
    agent = world.member(owner, "Ann Agent")
    P.create(agent, name="Hot Harry", temperature="hot")
    P.create(agent, name="Rita Recruit", kind="recruit")

    async def go():
        a = await sign_in(agent.user_email)
        try:
            for r in ("/prospects?tab=lists", "/prospects?tab=pipeline", "/prospects?tab=pipeline&kind=recruit",
                      "/prospects/list/hot", "/prospects/list/stale"):
                await a.goto(r)
                assert not a.has_text("This page isn't available"), r
            await a.goto("/prospects?tab=all&temp=hot")
            assert a.has_text("Hot Harry") and not a.has_text("Rita Recruit")
            await a.click("Save as list")
            await a.fill("List name", "My hot ones", in_dialog=True)
            await a.click("Save", in_dialog=True)
            await a.goto("/prospects?tab=lists")
            assert a.has_text("My hot ones")
            # Import from CSV through the menu.
            a.picked_files = [("leads.csv", b"Name,Phone,Email,Source\nNew Nick,9195550177,nick@example.com,event\n"
                                            b"Hot Harry Dup,,,\n")]
            menu_item = a.find(lambda x: isinstance(x, ft.PopupMenuItem)
                               and getattr(x.content, "value", "") == "Import from CSV")
            await a.fire(menu_item)
            await a.settle(quiet=0.6)
            assert a.has_text("Import 2 prospects"), a.texts()[-600:]
            await a.click("Import", in_dialog=True)
            assert a.has_text("Imported 2.")
            # Export: one-time signed download.
            item = a.find(lambda x: isinstance(x, ft.PopupMenuItem)
                          and getattr(x.content, "value", "") == "Export my prospects (CSV)")
            await a.fire(item)
            url = a.launched_urls[-1]
            path = "/" + url.split("/", 3)[3]
            r = httpx.get(a.base + path)
            assert r.status_code == 200 and "New Nick" in r.text and "nick@example.com" in r.text
            assert httpx.get(a.base + path).status_code == 403  # single use
        finally:
            await a.close()

    run(go())
