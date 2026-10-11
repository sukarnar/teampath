"""Tasks: personal to-dos and work delegated between an agent and their assistants."""

from __future__ import annotations

import uuid
from datetime import timedelta

import flet as ft

from ...services import orgs
from ...services import prospects as P
from ...services import tasks as T
from .. import components as C
from ..state import AppState, action

DUE_CHOICES = [("none", "No date"), ("0", "Today"), ("1", "Tomorrow"), ("3", "3 days"), ("7", "Next week"),
               ("custom", "Pick…")]
STATUS_ICON = {"open": ft.Icons.RADIO_BUTTON_UNCHECKED, "in_progress": ft.Icons.PENDING_OUTLINED,
               "done": ft.Icons.CHECK_CIRCLE, "cancelled": ft.Icons.CANCEL_OUTLINED}


def due_label(S: AppState, r) -> tuple[str, bool]:
    if not r.due_on:
        return "", False
    days = (r.due_on - S.ctx.today()).days
    txt = ("Due today" if days == 0 else "Due tomorrow" if days == 1 else
           f"{-days} day{'s' if days != -1 else ''} overdue" if days < 0 else f"Due {r.due_on:%a %b %d}")
    if r.due_time:
        txt += f" {r.due_time}"
    return txt, days < 0 and r.is_open


def task_row(S: AppState, r: T.TaskRow) -> ft.Control:
    due, late = due_label(S, r)
    who = ""
    if r.delegated:
        who = f"To {r.assignee_name}" if r.owner_id == S.user.id else f"From {r.owner_name}"
    parts = [x for x in (due, who, r.prospect_name and f"About {r.prospect_name}",
                         r.comments and f"{r.comments} comment{'s' if r.comments != 1 else ''}") if x]

    @action
    async def quick_done(e):
        await S.run(T.set_status, S.ctx, r.id, "done")
        S.toast(f"Done: {r.title}")
        await S.refresh()

    can_check = r.is_open and r.assignee_id == S.user.id
    lead = ft.IconButton(STATUS_ICON[r.status], tooltip="Mark done" if can_check else T.STATUSES[r.status],
                         on_click=quick_done if can_check else None,
                         icon_color=ft.Colors.PRIMARY if r.status == "done" else None)
    return ft.ListTile(
        leading=lead,
        title=ft.Text(r.title, weight=ft.FontWeight.W_600 if r.is_open else ft.FontWeight.W_400,
                      color=None if r.is_open else ft.Colors.ON_SURFACE_VARIANT),
        subtitle=ft.Text(" · ".join(parts), color=ft.Colors.ERROR if late else None) if parts else None,
        trailing=ft.Row([*([C.pill("High", bgcolor=ft.Colors.ERROR_CONTAINER, color=ft.Colors.ON_ERROR_CONTAINER,
                                   icon=ft.Icons.PRIORITY_HIGH)] if r.priority == "high" and r.is_open else []),
                         *([C.pill(T.STATUSES[r.status])] if r.status == "in_progress" else [])], tight=True),
        on_click=lambda e: S.go(f"/tasks/{r.id}"),
    )


def task_dialog(S: AppState, helpers: list[T.Helper], *, prospect_id: uuid.UUID | None = None,
                prospect_name: str | None = None, task: T.TaskDetail | None = None, default_title: str = "",
                default_assignee: uuid.UUID | None = None) -> None:
    title = C.text_field("Task", value=task.title if task else default_title,
                         hint_text="e.g. Confirm Tuesday's appointment with Maria")
    details = C.text_field("Instructions (optional)", value=(task.details or "") if task else "", multiline=True,
                           min_lines=2, max_lines=8)
    who_opts = [ft.DropdownOption(key=str(S.user.id), text="Me")] + \
               [ft.DropdownOption(key=str(h.user_id), text=h.name) for h in helpers]
    current = str(task.assignee_id) if task else str(default_assignee or (helpers[0].user_id if helpers else S.user.id))
    who = ft.Dropdown(label="Who does it", value=current, options=who_opts, border_radius=10, width=float("inf"),
                      visible=bool(helpers))
    due = ft.SegmentedButton(selected=["none" if not (task and task.due_on) else "custom"], show_selected_icon=False,
                             segments=[ft.Segment(value=k, label=ft.Text(v)) for k, v in DUE_CHOICES])
    picked = {"date": task.due_on if task else None}
    picked_label = ft.Text(f"Due {task.due_on:%a %b %d}" if task and task.due_on else "", size=12)
    time_f = C.text_field("Time (optional)", value=(task.due_time or "") if task else "", width=160, dense=True)
    high = ft.Switch(label="High priority", value=bool(task and task.priority == "high"))
    linked = ft.Text(f"About prospect: {prospect_name}", size=13, visible=bool(prospect_name))

    def on_due(e):
        if due.selected[0] == "custom":
            dp = ft.DatePicker(first_date=S.ctx.today(), last_date=S.ctx.today() + timedelta(days=730),
                               value=picked["date"] or S.ctx.today())

            def chosen(ev):
                v = ev.control.value
                picked["date"] = v.date() if hasattr(v, "date") else v
                picked_label.value = f"Due {picked['date']:%a %b %d, %Y}"
                picked_label.update()

            dp.on_change = chosen
            S.page.show_dialog(dp)
        else:
            picked_label.value = ""
            picked_label.update()

    due.on_change = on_due

    async def submit():
        k = due.selected[0]
        due_on = None if k == "none" else (picked["date"] if k == "custom" else S.ctx.today() + timedelta(days=int(k)))
        if k == "custom" and not due_on:
            S.toast("Pick a due date.", error=True)
            return False
        assignee = uuid.UUID(who.value) if who.value else S.user.id
        prio = "high" if high.value else "normal"
        if task:
            await S.run(T.edit, S.ctx, task.id, title=title.value, details=details.value, due_on=due_on,
                        due_time=time_f.value or None, priority=prio, assignee_id=assignee)
            S.toast("Saved.")
            await S.refresh()
        else:
            tid = await S.run(T.create, S.ctx, title=title.value, details=details.value, assignee_id=assignee,
                              due_on=due_on, due_time=time_f.value or None, priority=prio, prospect_id=prospect_id)
            name = next((h.name for h in helpers if h.user_id == assignee), None)
            S.toast(f"Delegated to {name}. They've been notified." if name else "Task added.")
            S.go(f"/tasks/{tid}")

    fields = [title, details, who, ft.Text("Due", size=12), ft.Row([due], scroll=ft.ScrollMode.AUTO), picked_label,
              ft.Row([time_f, high], wrap=True, spacing=16), linked]
    if prospect_id and helpers:
        fields.append(ft.Text("While the task is open, your assistant can see this prospect's contact details and log "
                              "calls — not your private notes. Unlicensed assistants should stick to scheduling and "
                              "confirming, not discussing coverage.", size=12, color=ft.Colors.ON_SURFACE_VARIANT))
    C.form_dialog(S.page, "Edit task" if task else ("Delegate a task" if helpers else "New task"), fields,
                  action(submit), "Save" if task else ("Delegate" if helpers else "Add"), width=520)


def open_new_task(S: AppState, **kw) -> None:
    async def go():
        helpers = await S.run(T.my_assistants, S.ctx) if not S.ctx.is_assistant else []
        task_dialog(S, helpers, **kw)

    S.page.run_task(action(go))


async def tasks_view(S: AppState, q: dict) -> ft.Control:
    helpers = await S.run(T.my_assistants, S.ctx) if not S.ctx.is_assistant else []
    agents = await S.run(T.my_agents, S.ctx)
    delegating = bool(helpers) or bool(await S.run(T.delegated_by_me, S.ctx))
    tab = q.get("tab", "mine")
    segs = [ft.Segment(value="mine", label=ft.Text("My tasks"), icon=ft.Icon(ft.Icons.CHECKLIST))]
    if delegating:
        segs.append(ft.Segment(value="delegated", label=ft.Text("Delegated"), icon=ft.Icon(ft.Icons.FORWARD_TO_INBOX)))
    segs.append(ft.Segment(value="done", label=ft.Text("Done"), icon=ft.Icon(ft.Icons.TASK_ALT)))
    blocks: list[ft.Control] = [ft.Row([ft.SegmentedButton(selected=[tab], segments=segs, show_selected_icon=False,
                                                           on_change=lambda e: S.go(f"/tasks?tab={e.control.selected[0]}"))],
                                       scroll=ft.ScrollMode.AUTO)]
    today = S.ctx.today()

    def grouped(rows: list[T.TaskRow]) -> list[ft.Control]:
        groups = [("Overdue", [r for r in rows if r.due_on and r.due_on < today]),
                  ("Today", [r for r in rows if r.due_on == today]),
                  ("Upcoming", [r for r in rows if r.due_on and r.due_on > today]),
                  ("No date", [r for r in rows if not r.due_on])]
        out = []
        for name, rs in groups:
            if rs:
                out.append(C.section(f"{name} ({len(rs)})", C.card(ft.Column([task_row(S, r) for r in rs], spacing=0),
                                                                   padding=4)))
        return out

    if tab == "delegated" and delegating:
        who = q.get("who")
        rows = await S.run(T.delegated_by_me, S.ctx, False, uuid.UUID(who) if who else None)
        if len(helpers) > 1:
            blocks.append(ft.Dropdown(label="Assistant", value=who or "", width=260, border_radius=10, dense=True,
                                      options=[ft.DropdownOption(key="", text="All assistants")] +
                                      [ft.DropdownOption(key=str(h.user_id), text=f"{h.name} ({h.open_tasks} open)")
                                       for h in helpers],
                                      on_select=lambda e: S.go(f"/tasks?tab=delegated&who={e.control.value}")))
        blocks += grouped(rows) or [C.empty("Nothing delegated right now.", ft.Icons.FORWARD_TO_INBOX)]
    elif tab == "done":
        rows = await S.run(T.recently_done, S.ctx, 14)
        blocks.append(C.card(ft.Column([task_row(S, r) for r in rows] or [C.empty("Nothing finished in the last two weeks.")],
                                       spacing=0), padding=4))
    else:
        rows = await S.run(T.assigned_to_me, S.ctx)
        blocks += grouped(rows) or [C.empty("You're all clear." + ("" if S.ctx.is_assistant else
                                                                    " Add a task, or delegate one to an assistant."),
                                            ft.Icons.TASK_ALT)]

    # Who I work with
    if S.ctx.is_assistant or agents:
        blocks.append(C.section("Agents you assist", *[ft.ListTile(leading=C.avatar(a.name), title=ft.Text(a.name),
                                                                    subtitle=ft.Text(a.email)) for a in agents]
                                or [ft.Text("No one has linked you as their assistant yet.",
                                            color=ft.Colors.ON_SURFACE_VARIANT)]))
    if not S.ctx.is_assistant:
        blocks.append(C.section("Your assistants", *[ft.ListTile(
            leading=C.avatar(h.name), title=ft.Text(h.name),
            subtitle=ft.Text(f"{h.open_tasks} open task{'s' if h.open_tasks != 1 else ''} · {h.email}"),
            on_click=lambda e, i=h.user_id: S.go(f"/tasks?tab=delegated&who={i}")) for h in helpers]
            or [ft.Text("Add an assistant to start delegating.", color=ft.Colors.ON_SURFACE_VARIANT)],
            trailing=ft.TextButton("Manage assistants", icon=ft.Icons.MANAGE_ACCOUNTS_OUTLINED,
                                   on_click=lambda e: S.go("/tasks/assistants"))))

    async def live(event):
        if S.page.route.startswith("/tasks") and "/tasks/" not in S.page.route:
            await S.refresh()

    S.live_handlers["tasks"] = live
    actions = [ft.FilledButton("Delegate a task" if helpers else "New task", icon=ft.Icons.ADD_TASK,
                               on_click=lambda e: task_dialog(S, helpers))]
    return C.page_body(*blocks, title="Tasks",
                       subtitle="Your to-dos and work you share with your assistants" if not S.ctx.is_assistant
                       else "Work delegated to you", actions=actions)


async def task_view(S: AppState, q: dict, task_id: str) -> ft.Control:
    tid = uuid.UUID(task_id)
    t = await S.run(T.get, S.ctx, tid)
    mine_to_do = t.assignee_id == S.user.id
    i_own = t.owner_id == S.user.id
    helpers = await S.run(T.my_assistants, S.ctx) if i_own and not S.ctx.is_assistant else []
    prospect = await S.run(P.get_for_task, S.ctx, tid) if t.prospect_id else None
    due, late = due_label(S, t)

    @action
    async def set_status(e):
        await S.run(T.set_status, S.ctx, tid, e.control.data)
        await S.refresh()

    def done_dialog(e):
        note = C.text_field("Note for " + (t.owner_name if t.delegated and not i_own else "yourself") + " (optional)",
                            multiline=True, min_lines=2, max_lines=6)

        async def submit():
            await S.run(T.set_status, S.ctx, tid, "done", note.value)
            S.toast("Marked done." + (f" {t.owner_name} has been notified." if t.delegated and not i_own else ""))
            await S.refresh()

        C.form_dialog(S.page, "Mark as done", [note], action(submit), "Done")

    def delete(e):
        async def yes():
            await S.run(T.delete, S.ctx, tid)
            S.go("/tasks")

        C.confirm(S.page, "Delete this task?", "It's removed for both of you.", action(yes), "Delete", danger=True)

    buttons: list[ft.Control] = []
    if t.is_open and mine_to_do:
        if t.status == "open" and t.delegated:
            buttons.append(ft.OutlinedButton("Start", icon=ft.Icons.PLAY_ARROW, data="in_progress", on_click=set_status))
        buttons.append(ft.FilledButton("Mark done", icon=ft.Icons.CHECK, on_click=done_dialog))
    if not t.is_open and (mine_to_do or i_own):
        buttons.append(ft.OutlinedButton("Reopen", icon=ft.Icons.REPLAY, data="open", on_click=set_status))
    menu_items = []
    if i_own:
        menu_items.append(ft.PopupMenuItem(content=ft.Text("Edit / reassign"), icon=ft.Icons.EDIT_OUTLINED,
                                           on_click=lambda e: task_dialog(S, helpers, task=t)))
        if t.is_open and t.delegated:
            menu_items.append(ft.PopupMenuItem(content=ft.Text("Cancel task"), icon=ft.Icons.CANCEL_OUTLINED,
                                               data="cancelled", on_click=set_status))
        menu_items.append(ft.PopupMenuItem(content=ft.Text("Delete"), icon=ft.Icons.DELETE_OUTLINE, on_click=delete))

    meta = [C.pill(T.STATUSES[t.status], icon=STATUS_ICON[t.status])]
    if t.priority == "high":
        meta.append(C.pill("High priority", bgcolor=ft.Colors.ERROR_CONTAINER, color=ft.Colors.ON_ERROR_CONTAINER))
    if due:
        meta.append(C.pill(due, bgcolor=ft.Colors.ERROR_CONTAINER if late else ft.Colors.SECONDARY_CONTAINER,
                           color=ft.Colors.ON_ERROR_CONTAINER if late else ft.Colors.ON_SECONDARY_CONTAINER))
    who = (f"Delegated by {t.owner_name} to {t.assignee_name}" if t.delegated else "Your own task")
    blocks: list[ft.Control] = [
        ft.Row([ft.TextButton("Tasks", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/tasks")),
                ft.Container(expand=True),
                *([ft.PopupMenuButton(icon=ft.Icons.MORE_VERT, items=menu_items)] if menu_items else [])]),
        ft.Row(meta, wrap=True, spacing=6),
        ft.Text(who + f" · created {C.when(t.created_at)}", color=ft.Colors.ON_SURFACE_VARIANT),
    ]
    if t.details:
        blocks.append(C.card(C.section("Instructions", ft.Text(t.details, selectable=True))))
    if t.completion_note:
        blocks.append(C.card(C.section("Done note", ft.Text(t.completion_note, selectable=True)),
                             bgcolor=ft.Colors.SECONDARY_CONTAINER))
    if buttons:
        blocks.append(ft.Row(buttons, wrap=True))

    if prospect:
        from .prospects import contact_buttons, log_panel, temp_pill, timeline

        pcard = [
            ft.Row([C.avatar(prospect.name), ft.Column([
                ft.Text(prospect.name, weight=ft.FontWeight.W_600, size=16),
                ft.Text(f"{prospect.stage_label} · next: {prospect.next_action or '—'}"
                        + (f" {prospect.next_on:%b %d}" if prospect.next_on else ""), size=12,
                        color=ft.Colors.ON_SURFACE_VARIANT)], spacing=0, expand=True), temp_pill(prospect.temperature)]),
            *([ft.Text(P.format_phone(prospect.phone), selectable=True)] if prospect.phone else []),
            *([ft.Text(prospect.email, selectable=True)] if prospect.email else []),
            ft.Row(contact_buttons(prospect), wrap=True),
            *([ft.Text("Do not call", color=ft.Colors.ERROR)] if prospect.dnc else []),
            *([ft.Text(f"Best time: {prospect.best_time}", size=12)] if prospect.best_time else []),
            ft.Text("Recent", size=12, color=ft.Colors.ON_SURFACE_VARIANT), *timeline(prospect, 3),
        ]
        if i_own:
            pcard.append(ft.TextButton("Open prospect", icon=ft.Icons.OPEN_IN_NEW,
                                       on_click=lambda e: S.go(f"/prospects/{prospect.id}")))
        blocks.append(C.card(C.section("Prospect", *pcard)))
        if mine_to_do and not i_own and t.is_open:
            async def logged():
                await S.refresh()

            blocks.append(C.card(C.section(f"Log a contact for {t.owner_name}",
                                           log_panel(S, prospect, logged, task_id=tid, save_label="Save"))))
    elif t.prospect_id and not prospect and not i_own:
        blocks.append(ft.Text("The prospect for this task is no longer available to you.",
                              color=ft.Colors.ON_SURFACE_VARIANT))

    # Comments
    msgs = []
    for c in t.thread:
        mine = c.author_id == S.user.id
        bubble = ft.Container(ft.Column([
            ft.Row([ft.Text(c.author_name, weight=ft.FontWeight.W_600, size=13),
                    ft.Text(C.when(c.created_at), size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=8),
            ft.Text(c.body, selectable=True)], spacing=4), padding=12, border_radius=12, width=560,
            bgcolor=ft.Colors.PRIMARY_CONTAINER if mine else ft.Colors.SURFACE_CONTAINER_HIGH)
        msgs.append(ft.Row([bubble], alignment=ft.MainAxisAlignment.END if mine else ft.MainAxisAlignment.START))
    body = C.text_field("Add a comment", multiline=True, min_lines=1, max_lines=6)

    @action
    async def send(e):
        await S.run(T.comment, S.ctx, tid, body.value)
        await S.refresh()

    if t.delegated:
        blocks.append(C.card(C.section("Comments", *(msgs or [ft.Text("No comments yet.",
                                                                     color=ft.Colors.ON_SURFACE_VARIANT)]),
                                       body, ft.Row([ft.FilledTonalButton("Send", icon=ft.Icons.SEND, on_click=send)]))))

    async def live(event):
        if event.get("task_id") == task_id and S.page.route == f"/tasks/{task_id}":
            await S.refresh()

    S.live_handlers["tasks"] = live
    return C.page_body(*blocks, title=t.title)


async def assistants_view(S: AppState, q: dict) -> ft.Control:
    helpers = await S.run(T.my_assistants, S.ctx) if not S.ctx.is_assistant else []
    agents = await S.run(T.my_agents, S.ctx)
    blocks: list[ft.Control] = [ft.TextButton("Tasks", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/tasks"))]

    @action
    async def remove(e):
        n = await S.run(T.unlink_assistant, S.ctx, e.control.data)
        S.toast("Removed." + (f" {n} open task(s) came back to you." if n else ""))
        await S.refresh()

    @action
    async def leave(e):
        n = await S.run(T.unlink_assistant, S.ctx, S.user.id, e.control.data)
        S.toast("You're no longer their assistant." + (f" {n} open task(s) went back to them." if n else ""))
        await S.refresh()

    if not S.ctx.is_assistant:
        def invite(e):
            email = C.text_field("Assistant's email", keyboard_type=ft.KeyboardType.EMAIL)

            async def submit():
                raw = await S.run(orgs.invite, S.ctx, email.value, "assistant")
                if not S.settings.smtp_host and not S.settings.is_production:
                    link = f"{S.settings.base_url}/invite?token={raw}"
                    S.page.show_dialog(ft.AlertDialog(title=ft.Text("Invitation created"), content=ft.Column([
                        ft.Text("Email isn't configured on this server, so share this link yourself:"),
                        ft.Text(link, selectable=True, size=12)], tight=True),
                        actions=[ft.TextButton("Done", on_click=lambda e: S.page.pop_dialog())]))
                else:
                    S.toast(f"Invitation sent to {email.value}.")

            C.form_dialog(S.page, "Invite an assistant", [
                email, ft.Text("They'll join as your assistant: they can do tasks you delegate, take training and ask "
                               "questions. They won't be part of your sales team or see your prospects, except one "
                               "you delegate a task about — and only while that task is open.", size=12,
                               color=ft.Colors.ON_SURFACE_VARIANT)], action(submit), "Send invitation")

        async def pick_existing():
            people = [p for p in await S.run(orgs.list_people, S.ctx)
                      if p.user_id != S.user.id and p.user_id not in {h.user_id for h in helpers}
                      and (S.ctx.is_admin or S.ctx.is_ancestor_of(p.path))]
            if not people:
                S.toast("There's no one in your team to choose. Invite an assistant instead.", error=True)
                return
            dd = ft.Dropdown(label="Person", border_radius=10, width=float("inf"), enable_filter=True,
                             options=[ft.DropdownOption(key=str(p.user_id), text=p.name) for p in people])

            async def submit():
                if not dd.value:
                    S.toast("Choose someone.", error=True)
                    return False
                await S.run(T.link_assistant, S.ctx, uuid.UUID(dd.value))
                S.toast("Added. They've been notified.")
                await S.refresh()

            C.form_dialog(S.page, "Add someone from your team", [dd], action(submit), "Add")

        blocks.append(C.card(C.section("Your assistants", *[ft.ListTile(
            leading=C.avatar(h.name), title=ft.Text(h.name), subtitle=ft.Text(f"{h.email} · {h.open_tasks} open"),
            trailing=ft.TextButton("Remove", data=h.user_id, on_click=remove)) for h in helpers]
            or [ft.Text("None yet.", color=ft.Colors.ON_SURFACE_VARIANT)],
            ft.Row([ft.FilledButton("Invite an assistant", icon=ft.Icons.PERSON_ADD_ALT, on_click=invite),
                    ft.OutlinedButton("Add someone from my team", icon=ft.Icons.GROUP_ADD_OUTLINED,
                                      on_click=lambda e: S.page.run_task(action(pick_existing)))], wrap=True))))
    if agents:
        blocks.append(C.card(C.section("Agents you assist", *[ft.ListTile(
            leading=C.avatar(a.name), title=ft.Text(a.name), subtitle=ft.Text(a.email),
            trailing=ft.TextButton("Stop assisting", data=a.user_id, on_click=leave)) for a in agents])))
    return C.page_body(*blocks, title="Assistants",
                       subtitle="Removing an assistant hands their open tasks back to you")
