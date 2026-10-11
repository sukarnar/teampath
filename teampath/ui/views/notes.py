"""Notes: take notes at trainings and conferences, pull out takeaways, share with the team."""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict

import flet as ft

from ...services import notes as N
from ...services import tasks as T
from .. import components as C
from ..state import AppState, action

MARKER_STYLE = {
    "key": (ft.Icons.STAR_OUTLINE, ft.Colors.PRIMARY, ft.Colors.with_opacity(0.12, ft.Colors.PRIMARY)),
    "action": (ft.Icons.CHECK_BOX_OUTLINED, ft.Colors.GREEN_700, ft.Colors.with_opacity(0.12, ft.Colors.GREEN)),
    "question": (ft.Icons.HELP_OUTLINE, ft.Colors.ORANGE_800, ft.Colors.with_opacity(0.14, ft.Colors.ORANGE)),
    "stat": (ft.Icons.INSERT_CHART_OUTLINED, ft.Colors.PURPLE_700, ft.Colors.with_opacity(0.12, ft.Colors.PURPLE)),
}
KIND_TITLES = {"key": "Key points", "action": "Action items", "question": "Questions to follow up",
               "stat": "Stats and quotes — verify before using"}


def render_body(body: str) -> ft.Control:
    """Shows note text with marked lines highlighted."""
    rows: list[ft.Control] = []
    for ln in body.splitlines():
        p = N.parse_line(ln)
        if p:
            icon, color, bg = MARKER_STYLE[p[0]]
            rows.append(ft.Container(ft.Row([ft.Icon(icon, color=color, size=18),
                                              ft.Text(p[1], selectable=True, expand=True)],
                                             vertical_alignment=ft.CrossAxisAlignment.START),
                                     bgcolor=bg, border_radius=8, padding=ft.Padding.symmetric(horizontal=10, vertical=6)))
        elif ln.strip():
            rows.append(ft.Text(ln, selectable=True))
        else:
            rows.append(ft.Container(height=4))
    return ft.Column(rows or [ft.Text("(empty)", color=ft.Colors.ON_SURFACE_VARIANT)], spacing=6)


async def open_url(S: AppState, url: str) -> None:
    await ft.UrlLauncher().launch_url(S.settings.base_url + url)


def marker_pills(m: dict[str, int]) -> list[ft.Control]:
    out = []
    for k, n in m.items():
        if n:
            icon, color, bg = MARKER_STYLE[k]
            out.append(C.pill(str(n), bgcolor=bg, color=color, icon=icon))
    return out


def _where(event_title, session_title, speaker) -> str:
    return " · ".join(x for x in (event_title, session_title, speaker and f"with {speaker}") if x)


def note_card(S: AppState, r: N.NoteRow) -> ft.Control:
    meta = [x for x in (_where(r.event_title, r.session_title, r.speaker), f"edited {C.when(r.updated_at)}") if x]
    pills = marker_pills(r.markers)
    if r.photos:
        pills.append(C.pill(str(r.photos), icon=ft.Icons.PHOTO_OUTLINED))
    if r.shared:
        pills.append(C.pill("Shared", icon=ft.Icons.GROUP_OUTLINED))
    pills += [C.pill(t, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH, color=ft.Colors.ON_SURFACE) for t in r.tags[:3]]
    return C.card(ft.Column([
        ft.Text(r.title, weight=ft.FontWeight.W_600, size=16),
        ft.Text(" · ".join(meta), size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        *([ft.Text(r.snippet, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)] if r.snippet else []),
        *([ft.Row(pills, wrap=True, spacing=6, run_spacing=6)] if pills else []),
    ], spacing=6), on_click=lambda e: S.go(f"/notes/{r.id}"), width=float("inf"))


def shared_row(S: AppState, r: N.SharedRow) -> ft.Control:
    how = {"you": "Shared with you", "team": "Shared with the team", "mine": "Your notes"}[r.how]
    parts = [x for x in (r.session_title, r.speaker and f"with {r.speaker}", how, C.when(r.updated_at),
                         r.comments and f"{r.comments} comment{'s' if r.comments != 1 else ''}") if x]
    return ft.ListTile(leading=C.avatar(r.author_name), title=ft.Text(f"{r.title} — {r.author_name}"),
                       subtitle=ft.Text(" · ".join(parts) + (f"\n{r.snippet}" if r.snippet else ""), max_lines=3),
                       on_click=lambda e: S.go(f"/notes/shared/{r.id}"))


def grouped_shared(S: AppState, rows: list[N.SharedRow]) -> list[ft.Control]:
    groups: OrderedDict[str, list[N.SharedRow]] = OrderedDict()
    for r in rows:
        groups.setdefault(r.group_key, []).append(r)
    out = []
    for _, rs in groups.items():
        title = rs[0].event_title or "Other notes"
        people = len({r.author_id for r in rs})
        sub = f"{len(rs)} note{'s' if len(rs) != 1 else ''} from {people} {'people' if people != 1 else 'person'}"
        out.append(C.card(C.section(title, ft.Text(sub, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                                    ft.Column([shared_row(S, r) for r in rs], spacing=0)), padding=12))
    return out


# ------------------------------------------------------------ dialogs

def _date_button(S: AppState, label: str, state: dict, key: str) -> ft.Control:
    txt = ft.Text(f"{label}: {state[key]:%b %d, %Y}" if state.get(key) else f"{label}: not set")

    def pick(e):
        dp = ft.DatePicker(value=state.get(key) or S.ctx.today())

        def chosen(ev):
            v = ev.control.value
            state[key] = v.date() if hasattr(v, "date") else v
            txt.value = f"{label}: {state[key]:%b %d, %Y}"
            txt.update()

        dp.on_change = chosen
        S.page.show_dialog(dp)

    return ft.OutlinedButton(content=txt, icon=ft.Icons.EVENT, on_click=pick)


def event_dialog(S: AppState, ev: N.EventRow | None = None) -> None:
    title = C.text_field("Event name", value=ev.title if ev else "", hint_text="e.g. Q4 Leadership Summit")
    dates = {"start": ev.starts_on if ev else S.ctx.today(), "end": ev.ends_on if ev else None}
    location = C.text_field("Location or link (optional)", value=(ev.location or "") if ev else "")
    organizer = C.text_field("Organizer (optional)", value=(ev.organizer or "") if ev else "")
    team = ft.Switch(label="Team event — my team can add their notes to it", value=False,
                     visible=ev is None and S.ctx.is_author)
    whole = ft.Switch(label="Whole organization", value=False, visible=ev is None and S.ctx.is_admin)

    async def submit():
        if ev:
            await S.run(N.update_event, S.ctx, ev.id, title.value, dates["start"], dates["end"], location.value,
                        organizer.value)
            S.toast("Saved.")
            await S.refresh()
        else:
            eid = await S.run(N.create_event, S.ctx, title.value, dates["start"], dates["end"], location.value,
                              organizer.value, for_team=team.value, whole_org=whole.value)
            S.toast("Event created." + (" Your team can now pick it when they take notes." if team.value or whole.value
                                        else ""))
            S.go(f"/notes/events/{eid}")

    C.form_dialog(S.page, "Edit event" if ev else "New event", [
        title, ft.Row([_date_button(S, "Starts", dates, "start"), _date_button(S, "Ends", dates, "end")], wrap=True),
        location, organizer, team, whole], action(submit), "Save" if ev else "Create")


def session_dialog(S: AppState, event_id: uuid.UUID, s: N.SessionRow | None = None) -> None:
    title = C.text_field("Session or talk", value=s.title if s else "", hint_text="e.g. Closing with confidence")
    speaker = C.text_field("Speaker (optional)", value=(s.speaker or "") if s else "")
    at = C.text_field("Time (optional)", value=(s.starts_at or "") if s else "", width=160, hint_text="2:30pm")

    async def submit():
        if s:
            await S.run(N.update_session, S.ctx, s.id, title.value, speaker.value, at.value)
        else:
            await S.run(N.add_session, S.ctx, event_id, title.value, speaker.value, at.value)
        await S.refresh()

    C.form_dialog(S.page, "Edit session" if s else "Add a session", [title, speaker, at], action(submit),
                  "Save" if s else "Add")


async def new_note(S: AppState, event_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None) -> None:
    nid = await S.run(N.create_note, S.ctx, "", event_id, session_id)
    S.go(f"/notes/{nid}")


def new_note_dialog(S: AppState) -> None:
    async def go():
        events = await S.run(N.list_events, S.ctx)
        ev = ft.Dropdown(label="Event (optional)", value="", border_radius=10, width=float("inf"),
                         options=[ft.DropdownOption(key="", text="No event — quick note")] +
                         [ft.DropdownOption(key=str(e.id), text=e.title + (f" ({e.dates})" if e.dates else ""))
                          for e in events])
        title = C.text_field("Title (optional)", hint_text="Defaults to the session or event name")

        async def submit():
            nid = await S.run(N.create_note, S.ctx, title.value, uuid.UUID(ev.value) if ev.value else None)
            S.go(f"/notes/{nid}")

        C.form_dialog(S.page, "New note", [ev, title], action(submit), "Start writing")

    S.page.run_task(action(go))


def share_dialog(S: AppState, d: N.NoteDetail) -> None:
    async def go():
        leader, has_team, people = await S.run(N.share_targets, S.ctx)
        cur = d.share
        to_leader = ft.Checkbox(label=f"My leader ({leader.name})" if leader else "My leader",
                                value=bool(cur and cur.to_leader) if cur else bool(leader), disabled=not leader)
        to_team = ft.Checkbox(label="My team (everyone below me)", value=bool(cur and cur.to_team), visible=has_team)
        picked = {u for u, _ in (cur.people if cur else [])}
        others = [p for p in people if not (leader and p.user_id == leader.user_id)]
        boxes = [ft.Checkbox(label=p.name, value=p.user_id in picked, data=p.user_id) for p in others]
        disclaimer = ft.Text("Notes tagged Financial concepts or Compliance show “" + N.DISCLAIMER + "” when "
                             "shared beyond your leader.", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                             visible=bool(N.DISCLAIMER_TAGS & {t.lower() for t in d.tags}))

        async def submit():
            sel = [b.data for b in boxes if b.value]
            await S.run(N.share, S.ctx, d.id, to_leader=bool(to_leader.value and leader), to_team=bool(to_team.value),
                        people=sel)
            S.toast("Shared. They've been notified." if not cur else "Sharing updated.")
            await S.refresh()

        fields: list[ft.Control] = [
            ft.Text("They get a copy of the note as it is now. Keep editing privately and update the shared copy "
                    "when you're ready.", size=13),
            to_leader, to_team]
        if boxes:
            fields += [ft.Text("Specific people", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                       ft.Column(boxes, spacing=0, height=min(240, 44 * len(boxes)), scroll=ft.ScrollMode.AUTO)]
        fields.append(disclaimer)
        C.form_dialog(S.page, "Share notes", fields, action(submit), "Share")

    S.page.run_task(action(go))


# ------------------------------------------------------------ list

async def notes_view(S: AppState, q: dict) -> ft.Control:
    tab = q.get("tab", "mine")
    segs = [ft.Segment(value="mine", label=ft.Text("My notes"), icon=ft.Icon(ft.Icons.EDIT_NOTE)),
            ft.Segment(value="shared", label=ft.Text("Shared"), icon=ft.Icon(ft.Icons.GROUP_OUTLINED)),
            ft.Segment(value="events", label=ft.Text("Events"), icon=ft.Icon(ft.Icons.EVENT_NOTE_OUTLINED))]
    blocks: list[ft.Control] = [ft.Row([ft.SegmentedButton(
        selected=[tab], segments=segs, show_selected_icon=False,
        on_change=lambda e: S.go(f"/notes?tab={e.control.selected[0]}"))], scroll=ft.ScrollMode.AUTO)]

    if tab == "shared":
        rows = await S.run(N.shared_with_me, S.ctx)
        mine = await S.run(N.my_shared, S.ctx)
        blocks += grouped_shared(S, rows) or [C.empty("Nothing shared with you yet. When your leader or teammates "
                                                      "share notes, they show up here, grouped by event.",
                                                      ft.Icons.GROUP_OUTLINED)]
        if mine:
            blocks.append(C.section("Notes you've shared", C.card(ft.Column([shared_row(S, r) for r in mine],
                                                                            spacing=0), padding=4)))
    elif tab == "events":
        events = await S.run(N.list_events, S.ctx)
        cards = []
        for e in events:
            bits = [x for x in (e.dates, e.location,
                                "Whole organization" if e.whole_org else (f"Team event · {e.owner_name}"
                                                                          if e.team_event else "Private"),
                                e.my_notes and f"{e.my_notes} of your notes") if x]
            cards.append(C.card(ft.Row([ft.Icon(ft.Icons.GROUPS if e.team_event else ft.Icons.EVENT_NOTE,
                                                color=ft.Colors.PRIMARY),
                                        ft.Column([ft.Text(e.title, weight=ft.FontWeight.W_600),
                                                   ft.Text(" · ".join(bits), size=12,
                                                           color=ft.Colors.ON_SURFACE_VARIANT)], spacing=2,
                                                  expand=True)]),
                                on_click=lambda ev, i=e.id: S.go(f"/notes/events/{i}")))
        blocks += cards or [C.empty("No events yet. Create one for a conference or training you're attending"
                                    + (", or a team event your whole team can use." if S.ctx.is_author else "."),
                                    ft.Icons.EVENT_NOTE_OUTLINED)]
    else:
        search = q.get("q", "")
        tag = q.get("tag", "")
        rows = await S.run(N.list_notes, S.ctx, search, None, tag or None)
        box = C.text_field("Search notes", value=search, prefix_icon=ft.Icons.SEARCH, dense=True, expand=True,
                           on_submit=lambda e: S.go(f"/notes?q={e.control.value}&tag={tag}"))
        tags = N.org_tags(S.profile.settings if S.profile else None)
        tag_dd = ft.Dropdown(label="Topic", value=tag, width=200, dense=True, border_radius=10,
                             options=[ft.DropdownOption(key="", text="All topics")] +
                             [ft.DropdownOption(key=t, text=t) for t in tags],
                             on_select=lambda e: S.go(f"/notes?q={search}&tag={e.control.value}"))
        blocks.append(ft.ResponsiveRow([ft.Container(box, col={"xs": 12, "md": 8}),
                                        ft.Container(tag_dd, col={"xs": 12, "md": 4})], run_spacing=8))
        blocks += [note_card(S, r) for r in rows] or [C.empty(
            "No notes match." if (search or tag) else
            "No notes yet. Start one when you're at a training or conference — mark key points, action items, "
            "questions and stats as you go.", ft.Icons.EDIT_NOTE)]

    async def live(event):
        if S.page.route.startswith("/notes") and "/notes/" not in S.page.route:
            await S.refresh()

    S.live_handlers["notes"] = live
    actions = [ft.FilledButton("New note", icon=ft.Icons.ADD, on_click=lambda e: new_note_dialog(S)),
               ft.OutlinedButton("Takeaways", icon=ft.Icons.LIGHTBULB_OUTLINE, on_click=lambda e: S.go("/notes/takeaways"))]
    if tab == "events":
        actions.insert(0, ft.FilledTonalButton("New event", icon=ft.Icons.EVENT, on_click=lambda e: event_dialog(S)))
    return C.page_body(*blocks, title="Notes", subtitle="From trainings, conferences and calls — private until you "
                       "share them", actions=actions)


# ------------------------------------------------------------ editor

async def note_view(S: AppState, q: dict, note_id: str) -> ft.Control:
    nid = uuid.UUID(note_id)
    d = await S.run(N.get_note, S.ctx, nid)
    events = await S.run(N.list_events, S.ctx)
    event = await S.run(N.get_event, S.ctx, d.event_id) if d.event_id else None
    st = {"seq": 0, "saved_seq": 0, "tags": list(d.tags)}
    status = ft.Text(f"Saved · {C.when(d.updated_at)}", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

    title = C.text_field("Title", value=d.title, text_size=18)
    body = ft.TextField(value=d.body, multiline=True, min_lines=14, max_lines=None, border_radius=10,
                        width=float("inf"), text_size=16,
                        hint_text="Write as you listen. Use the buttons below to mark key points, action items, "
                                  "questions and stats.")

    async def save_now():
        if st["saved_seq"] == st["seq"]:
            return
        seq = st["seq"]
        status.value = "Saving…"
        status.update()
        try:
            await S.run(N.save_note, S.ctx, nid, title=title.value or d.title, body=body.value or "",
                        tags=st["tags"])
        except Exception:
            status.value = "Not saved — check your connection"
            status.color = ft.Colors.ERROR
            status.update()
            raise
        st["saved_seq"] = max(st["saved_seq"], seq)
        status.value = "Saved" if st["saved_seq"] == st["seq"] else "Editing…"
        status.color = ft.Colors.ON_SURFACE_VARIANT
        status.update()

    async def autosave(seq):
        await asyncio.sleep(2.5)
        if seq == st["seq"]:
            await action(save_now)()

    def changed(e=None):
        st["seq"] += 1
        if status.value != "Editing…":
            status.value = "Editing…"
            status.update()
        S.page.run_task(autosave, st["seq"])

    async def flush(e=None):
        await save_now()

    title.on_change = changed
    body.on_change = changed
    title.on_blur = action(flush)
    body.on_blur = action(flush)

    def add_marker(e):
        prefix = N.MARKERS[e.control.data][0] + ": "
        v = body.value or ""
        body.value = v + ("" if not v or v.endswith("\n") else "\n") + prefix
        body.update()
        changed()
        S.page.run_task(body.focus)

    markers = ft.Row([ft.FilledTonalButton(N.MARKERS[k][1], icon=MARKER_STYLE[k][0], data=k, on_click=add_marker)
                      for k in N.MARKERS], wrap=True, spacing=8, run_spacing=8)

    # Event and session
    @action
    async def move_event(e):
        await S.run(N.save_note, S.ctx, nid, event_id=uuid.UUID(e.control.value) if e.control.value else None)
        await save_now()
        await S.refresh()

    @action
    async def move_session(e):
        await save_now()
        await S.run(N.save_note, S.ctx, nid, session_id=uuid.UUID(e.control.value) if e.control.value else None)
        await S.refresh()

    ev_dd = ft.Dropdown(label="Event", value=str(d.event_id) if d.event_id else "", dense=True, border_radius=10,
                        width=float("inf"), on_select=move_event,
                        options=[ft.DropdownOption(key="", text="No event")] +
                        [ft.DropdownOption(key=str(e.id), text=e.title) for e in events])
    se_dd = ft.Dropdown(label="Session", value=str(d.session_id) if d.session_id else "", dense=True,
                        border_radius=10, width=float("inf"), on_select=move_session, visible=bool(event),
                        options=[ft.DropdownOption(key="", text="No session")] +
                        [ft.DropdownOption(key=str(s.id), text=s.title + (f" — {s.speaker}" if s.speaker else ""))
                         for s in (event.sessions if event else [])])

    # Tags
    tag_row = ft.Row(wrap=True, spacing=6, run_spacing=6)
    all_tags = list(dict.fromkeys(N.org_tags(S.profile.settings if S.profile else None) + d.tags))

    def render_tags():
        tag_row.controls = [ft.Chip(label=ft.Text(t), selected=t in st["tags"], data=t, on_click=toggle_tag,
                                    show_checkmark=True) for t in all_tags]

    def toggle_tag(e):
        t = e.control.data
        st["tags"] = [x for x in st["tags"] if x != t] if t in st["tags"] else st["tags"] + [t]
        render_tags()
        tag_row.update()
        changed()

    def add_tag(e):
        t = (e.control.value or "").strip()[:40]
        if t and t.lower() not in {x.lower() for x in all_tags}:
            all_tags.append(t)
            st["tags"].append(t)
            render_tags()
            tag_row.update()
            changed()
        e.control.value = ""
        e.control.update()

    render_tags()
    new_tag = C.text_field("Add your own topic", width=220, dense=True, on_submit=add_tag)

    # Photos
    @action
    async def add_photo(e):
        got = await S.pick_and_upload(["jpg", "jpeg", "png"], "Choose a slide photo")
        if not got:
            return
        await save_now()
        await S.run(N.add_photo, S.ctx, nid, got[0], got[1])
        S.toast("Photo added.")
        await S.refresh()

    @action
    async def remove_photo(e):
        await save_now()
        await S.run(N.remove_photo, S.ctx, e.control.data)
        await S.refresh()

    photos = ft.Row([ft.Column([
        ft.Container(ft.Image(src=p.url, width=220, height=140, fit=ft.BoxFit.COVER), border_radius=8,
                     on_click=lambda e, u=p.url: S.page.run_task(open_url, S, u)),
        ft.Row([ft.Text(p.caption or "", size=12, expand=True, max_lines=1),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Remove photo", data=p.id, on_click=remove_photo)],
               width=220)], spacing=2) for p in d.photos], wrap=True, spacing=12, run_spacing=12)

    # Sharing
    @action
    async def refresh_share(e):
        await save_now()
        await S.run(N.refresh_share, S.ctx, nid)
        S.toast("Shared copy updated.")
        await S.refresh()

    @action
    async def stop_sharing(e):
        await S.run(N.unshare, S.ctx, nid)
        S.toast("No longer shared.")
        await S.refresh()

    async def open_share(e):
        await save_now()
        share_dialog(S, await S.run(N.get_note, S.ctx, nid))

    if d.share:
        who = (["your leader"] if d.share.to_leader else []) + (["your team"] if d.share.to_team else []) + \
            [n for _, n in d.share.people]
        share_box = C.card(C.section(
            "Shared", ft.Text("With " + ", ".join(who) + f" · copy from {C.when(d.share.updated_at)}"),
            *([ft.Text("You've edited the note since — they still see the earlier copy.", size=12,
                       color=ft.Colors.ON_SURFACE_VARIANT)] if d.share.out_of_date else []),
            ft.Row([*([ft.FilledButton("Update shared copy", icon=ft.Icons.SYNC, on_click=refresh_share)]
                      if d.share.out_of_date else []),
                    ft.OutlinedButton("Change who sees it", icon=ft.Icons.GROUP_ADD_OUTLINED, on_click=action(open_share)),
                    ft.OutlinedButton("View shared copy", icon=ft.Icons.VISIBILITY_OUTLINED,
                                      on_click=lambda e: S.go(f"/notes/shared/{d.share.id}")),
                    ft.TextButton("Stop sharing", on_click=stop_sharing)], wrap=True)),
            bgcolor=ft.Colors.SECONDARY_CONTAINER)
    else:
        share_box = None

    def delete(e):
        async def yes():
            await S.run(N.delete_note, S.ctx, nid)
            S.go("/notes")

        C.confirm(S.page, "Delete this note?", "Its photos and any shared copy are deleted too.", action(yes), "Delete",
                  danger=True)

    async def done(e):
        await save_now()
        S.go(f"/notes/events/{d.event_id}" if d.event_id else "/notes")

    back = f"/notes/events/{d.event_id}" if d.event_id else "/notes"
    blocks: list[ft.Control] = [
        ft.Row([ft.TextButton(d.event_title or "Notes", icon=ft.Icons.ARROW_BACK,
                              on_click=action(lambda e: _go_after_save(S, save_now, back))),
                ft.Container(expand=True), status,
                ft.PopupMenuButton(icon=ft.Icons.MORE_VERT, items=[
                    ft.PopupMenuItem(content=ft.Text("Takeaways from this note's event" if d.event_id else "Takeaways"),
                                     icon=ft.Icons.LIGHTBULB_OUTLINE,
                                     on_click=lambda e: S.page.run_task(action(lambda: _go_after_save(
                                         S, save_now, "/notes/takeaways" + (f"?event={d.event_id}" if d.event_id
                                                                            else ""))))),
                    ft.PopupMenuItem(content=ft.Text("Delete note"), icon=ft.Icons.DELETE_OUTLINE, on_click=delete)])]),
        ft.ResponsiveRow([ft.Container(ev_dd, col={"xs": 12, "md": 6}), ft.Container(se_dd, col={"xs": 12, "md": 6})],
                         run_spacing=8),
        title,
        markers,
        body,
        ft.Row([ft.FilledButton("Done", icon=ft.Icons.CHECK, on_click=action(done)),
                ft.OutlinedButton("Share…", icon=ft.Icons.IOS_SHARE, on_click=action(open_share)),
                ft.OutlinedButton("Add slide photo", icon=ft.Icons.ADD_A_PHOTO_OUTLINED, on_click=add_photo)],
               wrap=True),
        *([C.section(f"Slide photos ({len(d.photos)})", photos)] if d.photos else []),
        *([share_box] if share_box else []),
        C.section("Topics", tag_row, new_tag),
        ft.Text("Marked lines start with KEY:, ACTION:, Q: or STAT: — you can also type them yourself. "
                "Notes save automatically and are private until you share them.", size=12,
                color=ft.Colors.ON_SURFACE_VARIANT),
    ]
    return C.page_body(*blocks)


async def _go_after_save(S: AppState, save_now, route: str) -> None:
    await save_now()
    S.go(route)


# ------------------------------------------------------------ event

async def event_view(S: AppState, q: dict, event_id: str) -> ft.Control:
    eid = uuid.UUID(event_id)
    e = await S.run(N.get_event, S.ctx, eid)
    mine = await S.run(N.list_notes, S.ctx, "", eid)
    team = await S.run(N.shared_with_me, S.ctx, eid)
    owner = e.owner_id == S.user.id
    by_session: dict = {}
    for r in mine:
        by_session.setdefault(r.session_id, []).append(r)

    def delete(ev):
        async def yes():
            await S.run(N.delete_event, S.ctx, eid)
            S.go("/notes?tab=events")

        C.confirm(S.page, "Delete this event?", "Sessions are deleted. Everyone's notes are kept, without the event.",
                  action(yes), "Delete", danger=True)

    menu = []
    if owner:
        menu = [ft.PopupMenuItem(content=ft.Text("Edit event"), icon=ft.Icons.EDIT_OUTLINED,
                                 on_click=lambda ev: event_dialog(S, e)),
                ft.PopupMenuItem(content=ft.Text("Delete event"), icon=ft.Icons.DELETE_OUTLINE, on_click=delete)]
    meta = [x for x in (e.dates, e.location, e.organizer and f"by {e.organizer}") if x]
    kind = ("Whole organization" if e.whole_org else f"Team event from {e.owner_name}" if e.team_event
            else "Private event")

    session_rows = []
    for s in e.sessions:
        notes_here = by_session.get(s.id, [])
        can_edit = s.created_by == S.user.id or owner
        trailing = [ft.FilledTonalButton("Take notes" if not notes_here else "New note", icon=ft.Icons.EDIT_NOTE,
                                         on_click=lambda ev, i=s.id: S.page.run_task(action(lambda: new_note(S, eid, i))))]
        if can_edit:
            trailing.append(ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="Edit session",
                                          on_click=lambda ev, x=s: session_dialog(S, eid, x)))
        session_rows.append(ft.Column([
            ft.ResponsiveRow([
                ft.Container(ft.Column([ft.Text(s.title, weight=ft.FontWeight.W_600),
                                        ft.Text(" · ".join(x for x in (s.starts_at, s.speaker) if x), size=12,
                                                color=ft.Colors.ON_SURFACE_VARIANT)], spacing=2),
                             col={"xs": 12, "md": 8}),
                ft.Container(ft.Row(trailing, spacing=4, alignment=ft.MainAxisAlignment.END), col={"xs": 12, "md": 4})],
                vertical_alignment=ft.CrossAxisAlignment.CENTER),
            *[ft.TextButton(f"Your note: {r.title}", icon=ft.Icons.DESCRIPTION_OUTLINED,
                            on_click=lambda ev, i=r.id: S.go(f"/notes/{i}")) for r in notes_here],
            ft.Divider(height=1)], spacing=4))
    loose = by_session.get(None, [])

    blocks: list[ft.Control] = [
        ft.Row([ft.TextButton("Events", icon=ft.Icons.ARROW_BACK, on_click=lambda ev: S.go("/notes?tab=events")),
                ft.Container(expand=True), *([ft.PopupMenuButton(icon=ft.Icons.MORE_VERT, items=menu)] if menu else [])]),
        ft.Row([C.pill(kind, icon=ft.Icons.GROUPS if e.team_event else ft.Icons.LOCK_OUTLINE),
                *([ft.Text(" · ".join(meta), color=ft.Colors.ON_SURFACE_VARIANT)] if meta else [])], wrap=True),
        C.card(C.section("Sessions", *(session_rows or [ft.Text(
            "Add the talks you're attending so your notes stay organized" +
            (" — your team can add sessions too." if e.team_event else "."), color=ft.Colors.ON_SURFACE_VARIANT)]),
            trailing=ft.TextButton("Add session", icon=ft.Icons.ADD, on_click=lambda ev: session_dialog(S, eid)))),
    ]
    if loose:
        blocks.append(C.section("Your other notes from this event", *[note_card(S, r) for r in loose]))
    blocks.append(C.section(f"Shared by others ({len(team)})", C.card(ft.Column(
        [shared_row(S, r) for r in team] or [ft.Text("Nobody has shared notes from this event with you yet.",
                                                     color=ft.Colors.ON_SURFACE_VARIANT)], spacing=0), padding=8)))

    async def live(event):
        if S.page.route == f"/notes/events/{event_id}":
            await S.refresh()

    S.live_handlers["notes"] = live
    return C.page_body(*blocks, title=e.title, actions=[
        ft.FilledButton("New note", icon=ft.Icons.ADD, on_click=lambda ev: S.page.run_task(action(lambda: new_note(S, eid)))),
        ft.OutlinedButton("Takeaways", icon=ft.Icons.LIGHTBULB_OUTLINE,
                          on_click=lambda ev: S.go(f"/notes/takeaways?event={eid}"))])


# ------------------------------------------------------------ takeaways

async def takeaways_view(S: AppState, q: dict) -> ft.Control:
    eid = uuid.UUID(q["event"]) if q.get("event") else None
    ev = await S.run(N.get_event, S.ctx, eid) if eid else None
    items = await S.run(N.takeaways, S.ctx, eid)
    helpers = await S.run(T.my_assistants, S.ctx) if not S.ctx.is_assistant else []

    @action
    async def make_goal(e):
        t: N.Takeaway = e.control.data
        await S.run(N.make_goal, S.ctx, t.note_id, t.key)
        S.toast("Goal proposed to your leader." if not S.ctx.is_admin else "Goal added.")
        await S.refresh()

    def make_task(e):
        t: N.Takeaway = e.control.data
        if not helpers:
            async def mine():
                await S.run(N.make_task, S.ctx, t.note_id, t.key)
                S.toast("Added to your tasks.")
                await S.refresh()

            S.page.run_task(action(mine))
            return
        who = ft.Dropdown(label="Who does it", value=str(S.user.id), border_radius=10, width=float("inf"),
                          options=[ft.DropdownOption(key=str(S.user.id), text="Me")] +
                          [ft.DropdownOption(key=str(h.user_id), text=h.name) for h in helpers])

        async def submit():
            await S.run(N.make_task, S.ctx, t.note_id, t.key, uuid.UUID(who.value))
            S.toast("Task added." if who.value == str(S.user.id) else "Delegated. They've been notified.")
            await S.refresh()

        C.form_dialog(S.page, "Make it a task", [ft.Text(t.text, weight=ft.FontWeight.W_600), who], action(submit),
                      "Add task")

    sections = []
    for kind in N.MARKERS:
        rows = [t for t in items if t.kind == kind]
        if not rows:
            continue
        icon, color, bg = MARKER_STYLE[kind]
        lines = []
        for t in rows:
            src = " · ".join(x for x in (t.session_title or t.note_title, None if eid else t.event_title) if x)
            right: list[ft.Control] = []
            if kind == "action":
                if t.made == "goal":
                    right = [ft.TextButton("Became a goal", icon=ft.Icons.FLAG, on_click=lambda e: S.go("/goals"))]
                elif t.made == "task":
                    right = [ft.TextButton("Became a task", icon=ft.Icons.CHECKLIST,
                                           on_click=lambda e, i=t.target_id: S.go(f"/tasks/{i}"))]
                else:
                    right = [ft.OutlinedButton("Make goal", icon=ft.Icons.FLAG_OUTLINED, data=t, on_click=make_goal),
                             ft.OutlinedButton("Make task", icon=ft.Icons.ADD_TASK, data=t, on_click=make_task)]
            lines.append(ft.Container(ft.ResponsiveRow([
                ft.Container(ft.Row([ft.Icon(icon, color=color, size=18), ft.Column([
                    ft.Text(t.text, selectable=True),
                    ft.TextButton(src, style=ft.ButtonStyle(padding=0), on_click=lambda e, i=t.note_id: S.go(f"/notes/{i}"))],
                    spacing=0, expand=True)], vertical_alignment=ft.CrossAxisAlignment.START),
                    col={"xs": 12, "md": 8 if right else 12}),
                *([ft.Container(ft.Row(right, wrap=True, spacing=6, alignment=ft.MainAxisAlignment.END),
                                col={"xs": 12, "md": 4})] if right else [])],
                vertical_alignment=ft.CrossAxisAlignment.CENTER), bgcolor=bg, border_radius=10, padding=10))
        sections.append(C.section(f"{KIND_TITLES[kind]} ({len(rows)})", *lines))

    back = f"/notes/events/{eid}" if eid else "/notes"
    blocks: list[ft.Control] = [ft.TextButton(ev.title if ev else "Notes", icon=ft.Icons.ARROW_BACK,
                                              on_click=lambda e: S.go(back))]
    blocks += sections or [C.empty("No takeaways yet. In your notes, mark lines as Key point, Action item, Question "
                                   "or Stat and they're collected here.", ft.Icons.LIGHTBULB_OUTLINE)]
    return C.page_body(*blocks, title="Takeaways" + (f" — {ev.title}" if ev else ""),
                       subtitle="Everything you marked, in one place. Turn action items into goals or tasks.")


# ------------------------------------------------------------ shared copy

async def shared_view(S: AppState, q: dict, shared_id: str) -> ft.Control:
    sid = uuid.UUID(shared_id)
    s = await S.run(N.get_shared, S.ctx, sid)
    mine = s.author_id == S.user.id

    msgs = []
    for c in s.thread:
        me = c.author_id == S.user.id

        @action
        async def remove(e, cid=c.id):
            await S.run(N.delete_comment, S.ctx, cid)
            await S.refresh()

        bubble = ft.Container(ft.Column([
            ft.Row([ft.Text(c.author_name, weight=ft.FontWeight.W_600, size=13),
                    ft.Text(C.when(c.created_at), size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                    *([ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=16, tooltip="Delete", on_click=remove)]
                      if me else [])], spacing=8),
            ft.Text(c.body, selectable=True)], spacing=4), padding=12, border_radius=12, width=560,
            bgcolor=ft.Colors.PRIMARY_CONTAINER if me else ft.Colors.SURFACE_CONTAINER_HIGH)
        msgs.append(ft.Row([bubble], alignment=ft.MainAxisAlignment.END if me else ft.MainAxisAlignment.START))
    box = C.text_field("Add a comment or question", multiline=True, min_lines=1, max_lines=6)

    @action
    async def send(e):
        await S.run(N.comment, S.ctx, sid, box.value)
        await S.refresh()

    where = _where(s.event_title, s.session_title, s.speaker)
    blocks: list[ft.Control] = [
        ft.Row([ft.TextButton("Shared notes", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/notes?tab=shared")),
                ft.Container(expand=True),
                *([ft.OutlinedButton("Edit my note", icon=ft.Icons.EDIT_OUTLINED,
                                     on_click=lambda e: S.go(f"/notes/{s.note_id}"))] if mine else [])]),
        ft.Row([C.avatar(s.author_name), ft.Column([
            ft.Text(s.author_name, weight=ft.FontWeight.W_600),
            ft.Text(" · ".join(x for x in (where, f"shared {C.when(s.updated_at)}") if x), size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT)], spacing=0, expand=True)]),
    ]
    if mine:
        blocks.append(ft.Text(f"Shared with {s.audience}.", size=12, color=ft.Colors.ON_SURFACE_VARIANT))
    if s.disclaimer:
        blocks.append(C.card(ft.Row([ft.Icon(ft.Icons.INFO_OUTLINE, color=ft.Colors.ON_TERTIARY_CONTAINER),
                                     ft.Text(s.disclaimer, color=ft.Colors.ON_TERTIARY_CONTAINER, expand=True)]),
                             bgcolor=ft.Colors.TERTIARY_CONTAINER, padding=12))
    if s.tags:
        blocks.append(ft.Row([C.pill(t, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH, color=ft.Colors.ON_SURFACE)
                              for t in s.tags], wrap=True, spacing=6))
    blocks.append(C.card(render_body(s.body)))
    if s.photos:
        blocks.append(C.section("Slide photos", ft.Row([
            ft.Container(ft.Image(src=u, width=260, height=170, fit=ft.BoxFit.COVER), border_radius=8,
                         on_click=lambda e, x=u: S.page.run_task(open_url, S, x)) for u in s.photos], wrap=True, spacing=12,
            run_spacing=12)))
    blocks.append(C.card(C.section("Comments", *(msgs or [ft.Text("No comments yet. Ask a question or share what "
                                                                  "you'd add.", color=ft.Colors.ON_SURFACE_VARIANT)]),
                                   box, ft.Row([ft.FilledTonalButton("Send", icon=ft.Icons.SEND, on_click=send)]))))

    async def live(event):
        if event.get("shared_id") == shared_id and S.page.route == f"/notes/shared/{shared_id}":
            await S.refresh()

    S.live_handlers["notes"] = live
    return C.page_body(*blocks, title=s.title)
