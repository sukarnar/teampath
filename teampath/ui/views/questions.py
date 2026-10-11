"""Questions: inbox for leaders, my questions, and a thread page."""

from __future__ import annotations

import uuid

import flet as ft

from ...services import qa
from .. import components as C
from ..state import AppState, action

STATUS_STYLE = {
    "open": (ft.Colors.TERTIARY_CONTAINER, ft.Colors.ON_TERTIARY_CONTAINER, "Open"),
    "answered": (ft.Colors.SECONDARY_CONTAINER, ft.Colors.ON_SECONDARY_CONTAINER, "Answered"),
    "resolved": (ft.Colors.SURFACE_CONTAINER_HIGHEST, ft.Colors.ON_SURFACE_VARIANT, "Resolved"),
}


def status_pill(status: str) -> ft.Control:
    bg, fg, label = STATUS_STYLE.get(status, STATUS_STYLE["open"])
    return C.pill(label, bgcolor=bg, color=fg)


def thread_row(S: AppState, t: qa.ThreadView, show_author: bool) -> ft.Control:
    sub = (f"{t.author_name} · " if show_author else "") + f"{C.when(t.last_activity_at)}"
    if t.assigned_to_name and not show_author:
        sub += f" · with {t.assigned_to_name}"
    return C.card(ft.ListTile(leading=C.avatar(t.author_name) if show_author else ft.Icon(ft.Icons.HELP_OUTLINE),
                              title=ft.Text(t.title, weight=ft.FontWeight.W_500), subtitle=ft.Text(sub),
                              trailing=status_pill(t.status), on_click=lambda e: S.go(f"/questions/{t.id}")), padding=4)


def ask_dialog(S: AppState) -> None:
    title = C.text_field("Question (short)")
    body = C.text_field("Details", multiline=True, min_lines=3, max_lines=8)

    async def submit():
        tid = await S.run(qa.ask, S.ctx, title.value, body.value)
        S.go(f"/questions/{tid}")

    C.form_dialog(S.page, "Ask a question", [title, body], action(submit), "Ask")


async def questions_view(S: AppState, q: dict) -> ft.Control:
    tab = q.get("tab", "inbox" if S.has_team() else "mine")
    blocks: list[ft.Control] = []
    if S.has_team():
        blocks.append(ft.SegmentedButton(selected=[tab], segments=[
            ft.Segment(value="inbox", label=ft.Text("To answer"), icon=ft.Icon(ft.Icons.INBOX_OUTLINED)),
            ft.Segment(value="mine", label=ft.Text("My questions"), icon=ft.Icon(ft.Icons.PERSON_OUTLINE))],
            on_change=lambda e: S.go(f"/questions?tab={e.control.selected[0]}")))
    if tab == "inbox" and S.has_team():
        items = await S.run(qa.inbox, S.ctx)
        blocks += [thread_row(S, t, True) for t in items] or [C.empty("No open questions from your team.")]

        async def live(event):
            if S.page.route.startswith("/questions"):
                await S.refresh()

        S.live_handlers["qa"] = live
    else:
        items = await S.run(qa.my_questions, S.ctx)
        blocks += [thread_row(S, t, False) for t in items] or [C.empty("You haven't asked anything yet.")]
    return C.page_body(*blocks, title="Questions", subtitle="Ask anything — answers can become FAQs for everyone",
                       actions=[ft.FilledButton("Ask a question", icon=ft.Icons.ADD, on_click=lambda e: ask_dialog(S))])


async def thread_view(S: AppState, q: dict, thread_id: str) -> ft.Control:
    tid = uuid.UUID(thread_id)
    t = await S.run(qa.get_thread, S.ctx, tid)
    msgs = []
    for c in t.comments:
        mine = c.author_id == S.user.id
        bubble = ft.Container(ft.Column([
            ft.Row([ft.Text(c.author_name, weight=ft.FontWeight.W_600, size=13),
                    *([C.pill("Answer", icon=ft.Icons.CHECK)] if c.is_answer else []),
                    ft.Text(C.when(c.created_at), size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=8, wrap=True),
            ft.Text(c.body, selectable=True),
        ], spacing=4), padding=12, border_radius=12, width=620,
            bgcolor=ft.Colors.PRIMARY_CONTAINER if mine else ft.Colors.SURFACE_CONTAINER_HIGH)
        msgs.append(ft.Row([bubble], alignment=ft.MainAxisAlignment.END if mine else ft.MainAxisAlignment.START))
    reply = C.text_field("Write a reply", multiline=True, min_lines=2, max_lines=8)

    @action
    async def send(e):
        await S.run(qa.reply, S.ctx, tid, reply.value)
        await S.refresh()

    @action
    async def resolve(e):
        await S.run(qa.resolve, S.ctx, tid)
        S.toast("Marked as resolved.")
        await S.refresh()

    def promote(e):
        question = C.text_field("Question", value=t.title, multiline=True)
        last_answer = next((c.body for c in reversed(t.comments) if c.is_answer), "")
        answer = C.text_field("Answer", value=last_answer, multiline=True, min_lines=3, max_lines=10)

        async def submit():
            await S.run(qa.promote_to_faq, S.ctx, tid, question.value, answer.value)
            S.toast("Added to the FAQ.")

        C.form_dialog(S.page, "Add to FAQ", [ft.Text("Everyone who sees this step or material will see this answer."),
                                              question, answer], action(submit), "Add to FAQ", width=560)

    actions = []
    if t.can_promote and any(c.is_answer for c in t.comments):
        actions.append(ft.OutlinedButton("Add to FAQ", icon=ft.Icons.LIBRARY_ADD_OUTLINED, on_click=promote))
    if t.can_resolve:
        actions.append(ft.OutlinedButton("Mark resolved", icon=ft.Icons.TASK_ALT, on_click=resolve))
    context_link = []
    if t.target_type == "step" and t.target_id:
        context_link.append(ft.TextButton("Open the step", icon=ft.Icons.OPEN_IN_NEW,
                                          on_click=lambda e: S.go(f"/learn/step/{t.target_id}")))
    elif t.target_type == "content" and t.target_id:
        context_link.append(ft.TextButton("Open the material", icon=ft.Icons.OPEN_IN_NEW,
                                          on_click=lambda e: S.go(f"/content/{t.target_id}")))

    async def live(event):
        if event.get("thread_id") == thread_id and S.page.route == f"/questions/{thread_id}":
            await S.refresh()

    S.live_handlers["qa"] = live
    return C.page_body(
        ft.Row([ft.TextButton("All questions", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/questions")),
                *context_link, ft.Container(expand=True), status_pill(t.status)]),
        ft.Column(msgs, spacing=10),
        *([reply, ft.Row([ft.FilledButton("Reply", icon=ft.Icons.SEND, on_click=send)])] if t.status != "resolved" else
          [ft.Text("This question is resolved.", color=ft.Colors.ON_SURFACE_VARIANT)]),
        title=t.title, subtitle=f"Asked by {t.author_name}" + (f" · assigned to {t.assigned_to_name}"
                                                                if t.assigned_to_name else ""),
        actions=actions)
