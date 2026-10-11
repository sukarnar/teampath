"""Prospects: follow-ups, lists, pipeline, prospect detail and the focus session."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import flet as ft

from ...services import prospects as P
from .. import components as C
from ..state import AppState, action
from .tasks import open_new_task

TEMP_COLORS = {"hot": ("#C0392B", "#FFFFFF"), "warm": ("#C98A12", "#1F1400"), "cold": ("#5B7FA6", "#FFFFFF")}
TOUCH_ICONS = {"call": ft.Icons.CALL_OUTLINED, "text": ft.Icons.SMS_OUTLINED, "email": ft.Icons.MAIL_OUTLINE,
               "meeting": ft.Icons.HANDSHAKE_OUTLINED, "note": ft.Icons.STICKY_NOTE_2_OUTLINED}
NEXT_CHOICES = [("0", "Today"), ("1", "Tomorrow"), ("3", "3 days"), ("7", "Next week"), ("14", "2 weeks"),
                ("custom", "Pick…")]


# ------------------------------------------------------------ small pieces

def temp_pill(t: str) -> ft.Control:
    bg, fg = TEMP_COLORS.get(t, TEMP_COLORS["warm"])
    icon = {"hot": ft.Icons.LOCAL_FIRE_DEPARTMENT, "warm": ft.Icons.WB_SUNNY_OUTLINED, "cold": ft.Icons.AC_UNIT}.get(t)
    return C.pill(t.title(), bgcolor=bg, color=fg, icon=icon)


def kind_pill(kind: str, recruit_interest: bool = False) -> ft.Control:
    if kind == "recruit":
        return C.pill("Recruit", icon=ft.Icons.GROUP_ADD_OUTLINED)
    return C.pill("Sales + recruit" if recruit_interest else "Sales", icon=ft.Icons.SELL_OUTLINED)


def due_text(S: AppState, r) -> tuple[str, bool]:
    if r.closed:
        return r.stage_label, False
    if not r.next_on:
        return "No follow-up set", True
    today = S.ctx.today()
    days = (r.next_on - today).days
    when = ("today" if days == 0 else "tomorrow" if days == 1 else
            f"{-days} day{'s' if days != -1 else ''} overdue" if days < 0 else r.next_on.strftime("%a %b %d"))
    if r.next_time:
        when += f" at {r.next_time}"
    return f"{r.next_action or 'Follow up'} · {when}", days < 0


def prospect_row(S: AppState, r, on_click=None, selectable: dict | None = None) -> ft.Control:
    due, late = due_text(S, r)
    lead = C.avatar(r.name)
    if selectable is not None:
        cb = ft.Checkbox(value=r.id in selectable, data=r.id)

        def toggle(e):
            if e.control.value:
                selectable[e.control.data] = True
            else:
                selectable.pop(e.control.data, None)

        cb.on_change = toggle
        lead = ft.Row([cb, lead], tight=True, spacing=0)
    flags = []
    if r.dnc:
        flags.append(ft.Icon(ft.Icons.PHONE_DISABLED, size=16, color=ft.Colors.ERROR, tooltip="Do not call"))
    return ft.ListTile(
        leading=lead,
        title=ft.Text(r.name, weight=ft.FontWeight.W_600),
        subtitle=ft.Text(f"{r.stage_label} · {due}", color=ft.Colors.ERROR if late else None, max_lines=2),
        trailing=ft.Row([*flags, temp_pill(r.temperature)], tight=True, spacing=6),
        on_click=on_click or (lambda e, i=r.id: S.go(f"/prospects/{i}")),
    )


def _date_from_choice(S: AppState, choice: str, picked: date | None) -> date | None:
    if choice == "custom":
        return picked
    return S.ctx.today() + timedelta(days=int(choice))


def add_dialog(S: AppState, default_kind: str = "sales") -> None:
    name = C.text_field("Name")
    kind = ft.SegmentedButton(selected=[default_kind], segments=[
        ft.Segment(value="sales", label=ft.Text("Sales"), icon=ft.Icon(ft.Icons.SELL_OUTLINED)),
        ft.Segment(value="recruit", label=ft.Text("Recruiting"), icon=ft.Icon(ft.Icons.GROUP_ADD_OUTLINED))])
    phone = C.text_field("Phone", keyboard_type=ft.KeyboardType.PHONE)
    email = C.text_field("Email", keyboard_type=ft.KeyboardType.EMAIL)
    source = ft.Dropdown(label="Source", border_radius=10, width=float("inf"),
                         options=[ft.DropdownOption(key=k, text=v) for k, v in P.SOURCES])
    referred = C.text_field("Referred by (optional)")
    temp = ft.SegmentedButton(selected=["warm"], segments=[ft.Segment(value=k, label=ft.Text(v)) for k, v in P.TEMPERATURES])
    tags = C.text_field("Tags (comma separated)", hint_text="e.g. expo, small business")
    notes = C.text_field("Notes (optional)", multiline=True, min_lines=2, max_lines=5)
    first = ft.SegmentedButton(selected=["0"], segments=[ft.Segment(value=k, label=ft.Text(v)) for k, v in NEXT_CHOICES[:4]])
    dnc = ft.Checkbox(label="Do not call")
    no_text = ft.Checkbox(label="No text messages")
    recruit_int = ft.Checkbox(label="Might also be interested in joining the team")

    async def submit():
        try:
            pid = await S.run(P.create, S.ctx, name=name.value, kind=kind.selected[0], phone=phone.value,
                              email=email.value, source=source.value, referred_by=referred.value,
                              temperature=temp.selected[0], tags=tags.value, details=notes.value,
                              next_on=_date_from_choice(S, first.selected[0], None),
                              recruit_interest=recruit_int.value, dnc=dnc.value, no_text=no_text.value)
        except Exception as e:  # duplicate: offer to open the existing prospect
            if isinstance(getattr(e, "__cause__", None), P.DuplicateProspect):
                existing = e.__cause__.existing_id
                S.page.pop_dialog()
                S.go(f"/prospects/{existing}")
                return False
            raise
        S.toast("Prospect added.")
        S.go(f"/prospects/{pid}")

    C.form_dialog(S.page, "Add a prospect", [
        name, kind, phone, email, source, referred, ft.Text("Temperature", size=12), temp, tags, notes,
        ft.Text("First follow-up", size=12), first, ft.Row([dnc, no_text], wrap=True), recruit_int,
        ft.Text("Keep client details like policies, health or financial information in your carrier systems — "
                "not here.", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
    ], action(submit), "Add", width=520)


# ------------------------------------------------------------ log & next step (shared)

def log_panel(S: AppState, d: P.Detail, on_saved, list_id: uuid.UUID | None = None,
              save_label: str = "Save", task_id: uuid.UUID | None = None) -> ft.Control:
    """Choose what happened and what's next. Every open prospect leaves with a next step."""
    state = {"kind": "note" if d.dnc and d.no_text else ("text" if d.dnc else "call"), "outcome": None,
             "picked": None}
    kinds = [(k, v) for k, v in P.TOUCH_KINDS if not (k == "call" and d.dnc) and not (k == "text" and d.no_text)]
    kind_sel = ft.SegmentedButton(selected=[state["kind"]], show_selected_icon=False, segments=[
        ft.Segment(value=k, label=ft.Text(v), icon=ft.Icon(TOUCH_ICONS[k])) for k, v in kinds])
    chips = ft.Row(wrap=True, spacing=6, run_spacing=6)
    when = ft.SegmentedButton(selected=["1"], show_selected_icon=False,
                              segments=[ft.Segment(value=k, label=ft.Text(v)) for k, v in NEXT_CHOICES])
    picked_label = ft.Text("", size=12)
    time_f = C.text_field("Time", hint_text="14:30", width=110, dense=True)
    next_action = C.text_field("Next step", dense=True, expand=True)
    note = C.text_field("Note (optional)", multiline=True, min_lines=1, max_lines=5)
    next_box = ft.Column([ft.Text("When's the next follow-up?", weight=ft.FontWeight.W_500),
                          ft.Row([when], scroll=ft.ScrollMode.AUTO), picked_label,
                          ft.Row([next_action, time_f], spacing=8)], spacing=8)
    closed_msg = ft.Text("", color=ft.Colors.ON_SURFACE_VARIANT, visible=False)
    err = ft.Text("", color=ft.Colors.ERROR, visible=False)

    def suggest_choice(days: int) -> str:
        for k, _ in NEXT_CHOICES[:-1]:
            if int(k) >= days:
                return k
        return "custom"

    def render_chips():
        chips.controls = []
        for o in P.outcomes_for(state["kind"], d.kind):
            if task_id and o.key not in P.ASSISTANT_OUTCOMES:
                continue
            chips.controls.append(ft.Chip(label=ft.Text(o.label), selected=state["outcome"] == o.key, data=o.key,
                                          on_click=pick_outcome, show_checkmark=False))

    def pick_outcome(e):
        o = P.OUTCOME[e.control.data]
        state["outcome"] = o.key
        render_chips()
        closing = o.next_days is None
        next_box.visible = not closing
        closed_msg.visible = closing
        closed_msg.value = "This closes the prospect — no further follow-ups." if closing else ""
        if not closing and o.key != "note":
            k = suggest_choice(o.next_days)
            if k == "custom" and o.next_days:
                state["picked"] = S.ctx.today() + timedelta(days=o.next_days)
                picked_label.value = f"Suggested: {state['picked']:%a %b %d}"
            when.selected = [k]
            next_action.value = o.next_action
        elif o.key == "note":
            next_action.value = d.next_action or ""
        err.visible = False
        e.control.page.update()

    def on_kind(e):
        state["kind"] = kind_sel.selected[0]
        state["outcome"] = None
        render_chips()
        e.control.page.update()

    def on_when(e):
        if when.selected[0] == "custom":
            dp = ft.DatePicker(first_date=S.ctx.today(), last_date=S.ctx.today() + timedelta(days=730),
                               value=state["picked"] or S.ctx.today())

            def chosen(ev):
                v = ev.control.value
                state["picked"] = v.date() if hasattr(v, "date") else v
                picked_label.value = f"Picked: {state['picked']:%a %b %d, %Y}"
                picked_label.update()

            dp.on_change = chosen
            S.page.show_dialog(dp)
        else:
            picked_label.value = ""
            e.control.page.update()

    kind_sel.on_change = on_kind
    when.on_change = on_when
    render_chips()

    @action
    async def save(e):
        if not state["outcome"]:
            err.value, err.visible = "Choose what happened.", True
            err.update()
            return
        o = P.OUTCOME[state["outcome"]]
        nxt = None
        if o.next_days is not None and (o.key != "note" or when.selected[0] != "1" or state["picked"]):
            nxt = _date_from_choice(S, when.selected[0], state["picked"])
            if nxt is None:
                err.value, err.visible = "Pick a date for the next follow-up.", True
                err.update()
                return
        if o.key == "note" and when.selected[0] == "1" and not state["picked"]:
            nxt = None  # notes don't change the follow-up unless you pick a date
        res = await S.run(P.log_touch, S.ctx, d.id, kind=state["kind"], outcome=o.key, note=note.value,
                          next_on=nxt, next_action=next_action.value, next_time=time_f.value or None, list_id=list_id,
                          task_id=task_id)
        msg = "Saved." + (f" Counted: {', '.join(res.counted)}." if res.counted else "")
        S.toast(msg)
        await on_saved()

    return ft.Column([
        ft.Text("What happened?", weight=ft.FontWeight.W_500), ft.Row([kind_sel], scroll=ft.ScrollMode.AUTO),
        chips, note, next_box, closed_msg, err,
        ft.Row([ft.FilledButton(save_label, icon=ft.Icons.CHECK, on_click=save)]),
    ], spacing=10)


def contact_buttons(d: P.Detail) -> list[ft.Control]:
    """Tap-to-call / text / email open the agent's own phone or mail app. Nothing is sent by the platform."""
    out = []
    if d.phone:
        out.append(ft.FilledTonalButton("Call", icon=ft.Icons.CALL, url=f"tel:{d.phone}", disabled=d.dnc,
                                        tooltip="Do not call" if d.dnc else P.format_phone(d.phone)))
        out.append(ft.OutlinedButton("Text", icon=ft.Icons.SMS_OUTLINED, url=f"sms:{d.phone}", disabled=d.no_text,
                                     tooltip="No text messages" if d.no_text else None))
    if d.email:
        out.append(ft.OutlinedButton("Email", icon=ft.Icons.MAIL_OUTLINE, url=f"mailto:{d.email}"))
    return out


def timeline(d: P.Detail, limit: int | None = None) -> list[ft.Control]:
    rows = []
    for t in d.touches[:limit] if limit else d.touches:
        sub = C.when(t.created_at) + (f" · by {t.by_name}" if t.by_name else "")
        if t.stage_to:
            sub += f" · stage → {P.stage_label(d.kind, t.stage_to)}"
        rows.append(ft.ListTile(dense=True, leading=ft.Icon(TOUCH_ICONS.get(t.kind, ft.Icons.CIRCLE), size=20),
                                title=ft.Text(f"{t.kind.title()} — {t.outcome_label}" if t.outcome != "stage"
                                              else t.outcome_label),
                                subtitle=ft.Text((t.note + "\n" if t.note else "") + sub, max_lines=4)))
    return rows or [ft.Text("No activity yet.", color=ft.Colors.ON_SURFACE_VARIANT)]


# ------------------------------------------------------------ main page

async def prospects_view(S: AppState, q: dict) -> ft.Control:
    if S.ctx.is_assistant:
        S.go("/tasks")
        return None
    tab = q.get("tab", "due")
    tabs = ft.SegmentedButton(selected=[tab], show_selected_icon=False, segments=[
        ft.Segment(value="due", label=ft.Text("Follow-ups"), icon=ft.Icon(ft.Icons.ALARM)),
        ft.Segment(value="lists", label=ft.Text("Lists"), icon=ft.Icon(ft.Icons.LIST_ALT)),
        ft.Segment(value="pipeline", label=ft.Text("Pipeline"), icon=ft.Icon(ft.Icons.VIEW_KANBAN_OUTLINED)),
        ft.Segment(value="all", label=ft.Text("All"), icon=ft.Icon(ft.Icons.SEARCH))],
        on_change=lambda e: S.go(f"/prospects?tab={e.control.selected[0]}"))
    blocks: list[ft.Control] = [ft.Row([tabs], scroll=ft.ScrollMode.AUTO)]

    @action
    async def start(list_key: str):
        lid = await S.run(P.focus_from, S.ctx, list_key)
        S.go(f"/prospects/focus/{lid}")

    if tab == "due":
        overdue = await S.run(P.search, S.ctx, {"due": "overdue"})
        today = await S.run(P.search, S.ctx, {"due": "today"})
        week = [r for r in await S.run(P.search, S.ctx, {"due": "week"}) if r.next_on and r.next_on > S.ctx.today()]
        n = len(overdue) + len(today)
        if n:
            blocks.append(C.banner(
                ft.Icons.PLAY_CIRCLE_OUTLINE, f"{n} follow-up{'s' if n != 1 else ''} to do now",
                "Work through them one at a time: call, log what happened, set the next step.",
                [ft.FilledButton("Start follow-ups", icon=ft.Icons.PLAY_ARROW,
                                 on_click=lambda e: S.page.run_task(start, "due"))]))
        if overdue:
            blocks.append(C.section(f"Overdue ({len(overdue)})", *[prospect_row(S, r) for r in overdue]))
        blocks.append(C.section(f"Today ({len(today)})", *[prospect_row(S, r) for r in today]
                                or [ft.Text("Nothing else due today.", color=ft.Colors.ON_SURFACE_VARIANT)]))
        if week:
            blocks.append(C.section("Coming up this week", *[prospect_row(S, r) for r in week]))
        if not (overdue or today or week) and not await S.run(P.count, S.ctx, {}):
            blocks.append(C.empty("Add your first prospect, or import a list from a spreadsheet.",
                                  ft.Icons.PERSON_ADD_ALT))
    elif tab == "lists":
        infos = await S.run(P.lists, S.ctx)

        def list_card(i: P.ListInfo) -> ft.Control:
            if i.kind == "focus":
                sub = "Finished" if i.completed else f"{i.done} of {i.total} done"
                btn = ft.FilledTonalButton("Review" if i.completed else "Resume", icon=ft.Icons.PLAY_ARROW,
                                           on_click=lambda e, lid=i.id: S.go(f"/prospects/focus/{lid}"))
                bar = ft.ProgressBar(value=i.done / i.total if i.total else 0, bar_height=6)
            else:
                sub = (i.description + " · " if i.description else "") + f"{i.count} people"
                btn = ft.FilledTonalButton("Start", icon=ft.Icons.PLAY_ARROW, disabled=i.count == 0,
                                           on_click=lambda e, k=i.id: S.page.run_task(start, k))
                bar = None
            return ft.Container(C.card(ft.Column([
                ft.Row([ft.Text(i.name, weight=ft.FontWeight.W_600, expand=True), ft.Text(str(i.count), size=18,
                                                                                         weight=ft.FontWeight.W_700)]),
                ft.Text(sub, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                *([bar] if bar else []),
                ft.Row([btn, *([ft.TextButton("Open", on_click=lambda e, k=i.id: S.go(f"/prospects/list/{k}"))]
                               if i.kind != "focus" else [])]),
            ], spacing=6)), col={"xs": 12, "sm": 6, "lg": 4})

        built = [i for i in infos if i.kind == "builtin"]
        mine = [i for i in infos if i.kind == "smart"]
        focus = [i for i in infos if i.kind == "focus"]
        blocks.append(C.section("Ready-made lists", ft.ResponsiveRow([list_card(i) for i in built], spacing=12,
                                                                      run_spacing=12)))
        blocks.append(C.section("My saved lists", ft.ResponsiveRow([list_card(i) for i in mine], spacing=12,
                                                                    run_spacing=12) if mine else
                                ft.Text("Filter prospects under All, then save the filter as a list.",
                                        color=ft.Colors.ON_SURFACE_VARIANT)))
        blocks.append(C.section("Focus lists", ft.ResponsiveRow([list_card(i) for i in focus], spacing=12,
                                                                 run_spacing=12) if focus else
                                ft.Text("Start any list, or pick people under All, to create a list to work through now.",
                                        color=ft.Colors.ON_SURFACE_VARIANT)))
    elif tab == "pipeline":
        kind = q.get("kind", "sales")
        counts = await S.run(P.pipeline_counts, S.ctx, kind)
        rows = await S.run(P.search, S.ctx, {"kind": kind, "include_closed": True}, 1000)
        cols = []
        for stage, label, n in counts:
            people = [r for r in rows if r.stage == stage][:25]
            cols.append(ft.Container(ft.Column([
                ft.Row([ft.Text(label, weight=ft.FontWeight.W_600, expand=True), C.pill(str(n))]),
                *[ft.Container(ft.Column([ft.Text(r.name, weight=ft.FontWeight.W_500, max_lines=1),
                                          ft.Text(due_text(S, r)[0], size=11, max_lines=2,
                                                  color=ft.Colors.ERROR if due_text(S, r)[1]
                                                  else ft.Colors.ON_SURFACE_VARIANT)], spacing=2),
                               padding=10, border_radius=10, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, width=200,
                               on_click=lambda e, i=r.id: S.go(f"/prospects/{i}")) for r in people],
                *([ft.Text(f"+{n - len(people)} more", size=12)] if n > len(people) else []),
            ], spacing=8), width=220, padding=10, border_radius=12, bgcolor=ft.Colors.SURFACE_CONTAINER_LOW))
        blocks.append(ft.SegmentedButton(selected=[kind], segments=[
            ft.Segment(value="sales", label=ft.Text("Sales")), ft.Segment(value="recruit", label=ft.Text("Recruiting"))],
            on_change=lambda e: S.go(f"/prospects?tab=pipeline&kind={e.control.selected[0]}")))
        blocks.append(ft.Row(cols, scroll=ft.ScrollMode.AUTO, vertical_alignment=ft.CrossAxisAlignment.START,
                             spacing=12))
        blocks.append(ft.Text("Prospects move along as you log outcomes. Open one to change its stage by hand.",
                              size=12, color=ft.Colors.ON_SURFACE_VARIANT))
    else:
        blocks += await _all_tab(S, q)

    return C.page_body(*blocks, title="Prospects", subtitle="Your private follow-up list — only you can see it",
                       actions=[ft.FilledButton("Add prospect", icon=ft.Icons.PERSON_ADD_ALT,
                                                on_click=lambda e: add_dialog(S)),
                                _more_menu(S)])


def _more_menu(S: AppState) -> ft.Control:
    @action
    async def do_import(e):
        picked = await S.pick_and_upload(["csv"], "Choose a CSV file")
        if not picked:
            return
        path, _name = picked
        prev = await S.run(P.preview_csv, path)
        kind = ft.SegmentedButton(selected=["sales"], segments=[ft.Segment(value="sales", label=ft.Text("Sales")),
                                                                ft.Segment(value="recruit", label=ft.Text("Recruiting"))])
        tag = C.text_field("Tag everyone in this file (optional)", hint_text="e.g. expo 2026")
        mapped = "\n".join(f"• {field.replace('_', ' ').title()} ← “{hdr}”" for field, hdr in prev.columns.items())

        async def submit():
            res = await S.run(P.import_csv, S.ctx, path, kind.selected[0], tag.value)
            text = f"Imported {res.created}."
            if res.duplicates:
                text += f" Skipped {res.duplicates} you already have."
            if res.skipped:
                text += f" {res.skipped} row(s) couldn't be read."
            S.toast(text)
            await S.refresh()

        C.form_dialog(S.page, f"Import {prev.rows} prospects", [
            ft.Text("Columns we'll use:"), ft.Text(mapped, size=13),
            *([ft.Text("Ignored: " + ", ".join(prev.unmapped), size=12, color=ft.Colors.ON_SURFACE_VARIANT)]
              if prev.unmapped else []),
            ft.Text("Rows without a Type column are imported as:", size=12), kind, tag,
            ft.Text("Duplicates (same phone or email as someone you already have) are skipped.", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT),
        ], action(submit), "Import", width=480)

    @action
    async def do_export(e):
        url = await S.run(P.export_csv, S.ctx)
        await ft.UrlLauncher().launch_url(S.settings.base_url + url)
        S.toast("Your export is downloading. The link works once and expires in 5 minutes.")

    return ft.PopupMenuButton(icon=ft.Icons.MORE_VERT, tooltip="Import / export", items=[
        ft.PopupMenuItem(content=ft.Text("Import from CSV"), icon=ft.Icons.UPLOAD_FILE, on_click=do_import),
        ft.PopupMenuItem(content=ft.Text("Export my prospects (CSV)"), icon=ft.Icons.DOWNLOAD, on_click=do_export),
    ])


async def _all_tab(S: AppState, q: dict) -> list[ft.Control]:
    f: dict = {}
    if q.get("q"):
        f["text"] = q["q"]
    if q.get("kind") in P.KINDS:
        f["kind"] = q["kind"]
    if q.get("temp"):
        f["temperature"] = [q["temp"]]
    if q.get("stage"):
        f["stages"] = [q["stage"]]
    if q.get("source"):
        f["source"] = q["source"]
    if q.get("tag"):
        f["tags"] = [q["tag"]]
    if q.get("closed") == "1":
        f["include_closed"] = True
    if q.get("archived") == "1":
        f["archived"] = True
    rows = await S.run(P.search, S.ctx, f, 500)
    selected: dict = {}

    def go(**kw):
        params = {k: v for k, v in {**q, "tab": "all", **kw}.items() if v}
        S.go("/prospects?" + "&".join(f"{k}={v}" for k, v in params.items()))

    search = C.text_field("Search name, phone, email or tag", value=q.get("q", ""), prefix_icon=ft.Icons.SEARCH,
                          on_submit=lambda e: go(q=e.control.value))
    stages = sorted({(s, lbl) for k in P.KINDS for s, lbl in P.STAGES[k]}, key=lambda x: x[1])

    def dd(label, key, options, width=170):
        return ft.Dropdown(label=label, value=q.get(key) or "", width=width, border_radius=10, dense=True,
                           options=[ft.DropdownOption(key="", text="Any")] +
                           [ft.DropdownOption(key=k, text=v) for k, v in options],
                           on_select=lambda e: go(**{key: e.control.value}))

    filters = ft.Row([
        dd("Type", "kind", [("sales", "Sales"), ("recruit", "Recruiting")], 150),
        dd("Temperature", "temp", P.TEMPERATURES, 150),
        dd("Stage", "stage", stages),
        dd("Source", "source", P.SOURCES),
        ft.Switch(label="Include closed", value=q.get("closed") == "1",
                  on_change=lambda e: go(closed="1" if e.control.value else "")),
        ft.Switch(label="Archived", value=q.get("archived") == "1",
                  on_change=lambda e: go(archived="1" if e.control.value else "")),
    ], wrap=True, spacing=10, run_spacing=10)

    @action
    async def focus_selected(e):
        ids = list(selected) or [r.id for r in rows]
        name = C.text_field("List name", value=f"Focus – {S.ctx.today():%b %d}")

        async def submit():
            lid = await S.run(P.create_focus_list, S.ctx, name.value, ids[:300])
            S.go(f"/prospects/focus/{lid}")

        C.form_dialog(S.page, f"Work through {min(len(ids), 300)} prospect(s)", [name], action(submit), "Start")

    def save_filter(e):
        name = C.text_field("List name", hint_text="e.g. Warm small-business leads")

        async def submit():
            await S.run(P.save_smart_list, S.ctx, name.value, f)
            S.toast("Saved under Lists. It updates itself as prospects change.")

        C.form_dialog(S.page, "Save these filters as a list", [name], action(submit))

    return [search, filters,
            ft.Row([ft.Text(f"{len(rows)} prospect(s)", color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.OutlinedButton("Save as list", icon=ft.Icons.BOOKMARK_ADD_OUTLINED, on_click=save_filter,
                                      disabled=not f),
                    ft.FilledTonalButton("Focus on these", icon=ft.Icons.PLAY_ARROW, on_click=focus_selected,
                                         disabled=not rows)], wrap=True),
            ft.Text("Tick people to focus on just them.", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            C.card(ft.Column([prospect_row(S, r, selectable=selected) for r in rows]
                             or [C.empty("No prospects match.")], spacing=0), padding=4)]


async def list_view(S: AppState, q: dict, list_id: str) -> ft.Control:
    name, f = await S.run(P.resolve_filter, S.ctx, list_id)
    rows = await S.run(P.search, S.ctx, f, 500)

    @action
    async def start(e):
        lid = await S.run(P.focus_from, S.ctx, list_id)
        S.go(f"/prospects/focus/{lid}")

    @action
    async def delete(e):
        await S.run(P.delete_list, S.ctx, uuid.UUID(list_id))
        S.go("/prospects?tab=lists")

    actions = [ft.FilledButton("Start focus session", icon=ft.Icons.PLAY_ARROW, on_click=start, disabled=not rows)]
    if list_id not in P.BUILTIN:
        actions.append(ft.TextButton("Delete list", icon=ft.Icons.DELETE_OUTLINE, on_click=delete))
    return C.page_body(ft.TextButton("All lists", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/prospects?tab=lists")),
                       C.card(ft.Column([prospect_row(S, r) for r in rows] or [C.empty("No one on this list right now.")],
                                        spacing=0), padding=4),
                       title=name, subtitle=f"{len(rows)} prospect(s) · updates automatically", actions=actions)


# ------------------------------------------------------------ prospect detail

def prospect_tasks(ctx, prospect_id):
    from ...services import tasks as T

    return [r for r in T.delegated_by_me(ctx) if r.prospect_id == prospect_id]

async def detail_view(S: AppState, q: dict, prospect_id: str) -> ft.Control:
    pid = uuid.UUID(prospect_id)
    d = await S.run(P.get, S.ctx, pid)
    infos = [i for i in await S.run(P.lists, S.ctx) if i.kind == "focus" and not i.completed]

    @action
    async def set_stage(e):
        await S.run(P.set_stage, S.ctx, pid, e.control.value)
        await S.refresh()

    @action
    async def set_temp(e):
        await S.run(P.update_details, S.ctx, pid, temperature=e.control.selected[0])
        await S.refresh()

    @action
    async def toggle(e):
        await S.run(P.update_details, S.ctx, pid, **{e.control.data: e.control.value})
        await S.refresh()

    @action
    async def add_to_list(e):
        n = await S.run(P.add_to_focus, S.ctx, uuid.UUID(e.control.data), [pid])
        S.toast("Added." if n else "Already on that list.")

    @action
    async def new_focus(e):
        lid = await S.run(P.create_focus_list, S.ctx, f"Focus – {S.ctx.today():%b %d}", [pid])
        S.toast("New focus list created.")
        S.go(f"/prospects/focus/{lid}")

    def edit(e):
        f = {"name": C.text_field("Name", value=d.name), "phone": C.text_field("Phone", value=d.phone or ""),
             "email": C.text_field("Email", value=d.email or ""),
             "referred_by": C.text_field("Referred by", value=d.referred_by or ""),
             "best_time": C.text_field("Best time to reach", value=d.best_time or ""),
             "tags": C.text_field("Tags (comma separated)", value=", ".join(d.tags)),
             "consent_note": C.text_field("Contact permission note", value=d.consent_note or "",
                                          hint_text="e.g. Asked us to call after 6pm; gave number at expo"),
             "details": C.text_field("Notes", value=d.details or "", multiline=True, min_lines=3, max_lines=10)}
        source = ft.Dropdown(label="Source", value=d.source or "", border_radius=10, width=float("inf"),
                             options=[ft.DropdownOption(key="", text="—")] +
                             [ft.DropdownOption(key=k, text=v) for k, v in P.SOURCES])
        kind = ft.SegmentedButton(selected=[d.kind], segments=[ft.Segment(value="sales", label=ft.Text("Sales")),
                                                               ft.Segment(value="recruit", label=ft.Text("Recruiting"))])

        async def submit():
            await S.run(P.update_details, S.ctx, pid, **{k: v.value for k, v in f.items()}, source=source.value,
                        kind=kind.selected[0])
            await S.refresh()

        C.form_dialog(S.page, "Edit prospect", [kind, *f.values(), source], action(submit), width=520)

    def delete(e):
        async def yes():
            await S.run(P.delete_prospect, S.ctx, pid)
            S.toast("Prospect deleted.")
            S.go("/prospects")

        C.confirm(S.page, f"Delete {d.name}?", "This permanently removes them and their history.", action(yes),
                  "Delete", danger=True)

    @action
    async def archive(e):
        await S.run(P.set_archived, S.ctx, pid, not d.archived)
        await S.refresh()

    async def refresh():
        await S.refresh()

    due, late = due_text(S, d)
    header = ft.Row([
        C.avatar(d.name, 26),
        ft.Column([ft.Text(d.name, theme_style=ft.TextThemeStyle.HEADLINE_SMALL, weight=ft.FontWeight.W_600),
                   ft.Row([kind_pill(d.kind, d.recruit_interest), temp_pill(d.temperature),
                           *([C.pill("Archived")] if d.archived else [])], wrap=True)], spacing=4, expand=True),
        ft.PopupMenuButton(icon=ft.Icons.MORE_VERT, items=[
            ft.PopupMenuItem(content=ft.Text("Edit details"), icon=ft.Icons.EDIT_OUTLINED, on_click=edit),
            *[ft.PopupMenuItem(content=ft.Text(f"Add to “{i.name}”"), icon=ft.Icons.PLAYLIST_ADD, data=i.id,
                               on_click=add_to_list) for i in infos[:5]],
            ft.PopupMenuItem(content=ft.Text("Add to a new focus list"), icon=ft.Icons.PLAYLIST_ADD,
                             on_click=new_focus),
            ft.PopupMenuItem(content=ft.Text("Delegate a task about them"), icon=ft.Icons.FORWARD_TO_INBOX,
                             on_click=lambda e: open_new_task(S, prospect_id=pid, prospect_name=d.name,
                                                              default_title=f"Follow up with {d.name}")),
            ft.PopupMenuItem(content=ft.Text("Restore" if d.archived else "Archive"), icon=ft.Icons.ARCHIVE_OUTLINED,
                             on_click=archive),
            ft.PopupMenuItem(content=ft.Text("Delete"), icon=ft.Icons.DELETE_OUTLINE, on_click=delete),
        ]),
    ])
    stage_dd = ft.Dropdown(label="Stage", value=d.stage, border_radius=10, width=220, dense=True, on_select=set_stage,
                           options=[ft.DropdownOption(key=k, text=v) for k, v in P.STAGES[d.kind]])
    temp = ft.SegmentedButton(selected=[d.temperature], on_change=set_temp, show_selected_icon=False,
                              segments=[ft.Segment(value=k, label=ft.Text(v)) for k, v in P.TEMPERATURES])
    contact = C.card(ft.Column([
        ft.Text("Contact", weight=ft.FontWeight.W_600),
        *([ft.Text(P.format_phone(d.phone), selectable=True)] if d.phone else []),
        *([ft.Text(d.email, selectable=True)] if d.email else []),
        *([ft.Text("No phone or email yet — add them under Edit details.", color=ft.Colors.ON_SURFACE_VARIANT)]
          if not (d.phone or d.email) else []),
        ft.Row(contact_buttons(d), wrap=True),
        ft.Row([ft.Checkbox(label="Do not call", value=d.dnc, data="dnc", on_change=toggle),
                ft.Checkbox(label="No texts", value=d.no_text, data="no_text", on_change=toggle)], wrap=True),
        *([ft.Text(f"Permission: {d.consent_note}", size=12)] if d.consent_note else []),
        *([ft.Text(f"Best time: {d.best_time}", size=12)] if d.best_time else []),
        *([ft.Text(f"Referred by {d.referred_by}", size=12)] if d.referred_by else []),
        *([ft.Text(f"Source: {dict(P.SOURCES).get(d.source, d.source)}", size=12)] if d.source else []),
    ], spacing=6))
    next_card = C.card(ft.Row([
        ft.Icon(ft.Icons.ALARM, color=ft.Colors.ERROR if late else ft.Colors.PRIMARY),
        ft.Column([ft.Text("Next step", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                   ft.Text(due, weight=ft.FontWeight.W_600, color=ft.Colors.ERROR if late else None)],
                  spacing=0, expand=True),
    ]), bgcolor=ft.Colors.ERROR_CONTAINER if late else None)
    open_tasks = await S.run(prospect_tasks, S.ctx, pid)
    blocks = [header, ft.Row([stage_dd, temp], wrap=True, spacing=12), next_card]
    if open_tasks:
        blocks.append(C.card(C.section("Delegated", *[ft.ListTile(
            leading=ft.Icon(ft.Icons.FORWARD_TO_INBOX), title=ft.Text(r.title),
            subtitle=ft.Text(f"{r.assignee_name} · {r.status.replace('_', ' ')}"),
            on_click=lambda e, i=r.id: S.go(f"/tasks/{i}")) for r in open_tasks])))
    if S.is_wide:
        blocks.append(ft.Row([
            ft.Container(C.card(log_panel(S, d, refresh, save_label="Save & set next step")), expand=3),
            ft.Container(contact, expand=2)], vertical_alignment=ft.CrossAxisAlignment.START, spacing=12))
    else:
        blocks += [contact, C.card(log_panel(S, d, refresh, save_label="Save & set next step"))]
    if d.details:
        blocks.append(C.card(C.section("Notes", ft.Text(d.details, selectable=True))))
    blocks.append(C.card(C.section("History", *timeline(d))))
    return C.page_body(ft.TextButton("Prospects", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/prospects")),
                       *blocks)


# ------------------------------------------------------------ focus session

async def focus_view(S: AppState, q: dict, list_id: str) -> ft.Control:
    lid = uuid.UUID(list_id)
    st = await S.run(P.focus_state, S.ctx, lid)
    progress = ft.Column([
        ft.Row([ft.Text(st.name, weight=ft.FontWeight.W_600, expand=True), ft.Text(f"{st.done} of {st.total} done")]),
        ft.ProgressBar(value=st.done / st.total if st.total else 0, bar_height=8, border_radius=8)], spacing=6)
    back = ft.TextButton("Prospects", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/prospects?tab=lists"))

    if st.current is None:
        rows = [ft.ListTile(title=ft.Text(P.OUTCOME[k].label if k in P.OUTCOME else "Skipped"),
                            trailing=ft.Text(str(n), weight=ft.FontWeight.W_700)) for k, n in
                sorted(st.outcomes.items(), key=lambda kv: -kv[1])]
        return C.page_body(back, progress, C.card(ft.Column([
            ft.Row([ft.Icon(ft.Icons.EMOJI_EVENTS_OUTLINED, color=ft.Colors.PRIMARY, size=32),
                    ft.Text("List finished — nice work.", theme_style=ft.TextThemeStyle.TITLE_LARGE)]),
            *rows,
            ft.Row([ft.FilledButton("Back to follow-ups", on_click=lambda e: S.go("/prospects")),
                    ft.OutlinedButton("Today", on_click=lambda e: S.go("/"))], wrap=True),
        ], spacing=8)), title="Focus session")

    d = st.current
    due, late = due_text(S, d)

    async def saved():
        await S.refresh()

    @action
    async def skip(e):
        await S.run(P.skip, S.ctx, lid, d.id)
        await S.refresh()

    person = C.card(ft.Column([
        ft.Row([C.avatar(d.name, 24), ft.Column([
            ft.Text(d.name, theme_style=ft.TextThemeStyle.HEADLINE_SMALL, weight=ft.FontWeight.W_600),
            ft.Row([kind_pill(d.kind, d.recruit_interest), C.pill(d.stage_label), temp_pill(d.temperature)], wrap=True),
        ], spacing=4, expand=True)]),
        ft.Text(due, color=ft.Colors.ERROR if late else ft.Colors.ON_SURFACE_VARIANT),
        *([ft.Text(P.format_phone(d.phone), selectable=True, size=18, weight=ft.FontWeight.W_500)] if d.phone else []),
        ft.Row(contact_buttons(d), wrap=True),
        *([ft.Text("Do not call", color=ft.Colors.ERROR)] if d.dnc else []),
        *([ft.Text(d.details, max_lines=4, size=13)] if d.details else []),
        ft.Text("Recent", size=12, color=ft.Colors.ON_SURFACE_VARIANT), *timeline(d, 3),
        ft.Row([ft.TextButton("Open full profile", on_click=lambda e: S.go(f"/prospects/{d.id}")),
                ft.TextButton("Skip for now", icon=ft.Icons.SKIP_NEXT, on_click=skip)], wrap=True),
    ], spacing=8))
    panel = C.card(log_panel(S, d, saved, list_id=lid, save_label="Save & next"))
    up_next = ft.Text("Up next: " + ", ".join(st.remaining_names), size=12, color=ft.Colors.ON_SURFACE_VARIANT) \
        if st.remaining_names else ft.Container()
    if S.is_wide:
        body = ft.Row([ft.Container(person, expand=2), ft.Container(panel, expand=3)],
                      vertical_alignment=ft.CrossAxisAlignment.START, spacing=12)
    else:
        body = ft.Column([person, panel], spacing=12)
    return C.page_body(back, progress, body, up_next, title="Focus session")
