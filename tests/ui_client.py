"""A minimal headless Flet client for end-to-end UI tests.

It speaks Flet's websocket protocol (msgpack), answers the client-side method
calls the app makes (navigation, shared preferences, file picker), and drives
the real server-side controls: it finds them in the live session, sets field
values and fires click/change/submit events exactly like the browser would.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import socket
import threading
import time
from typing import Any, Callable

import httpx
import msgpack
import uvicorn
import websockets

import flet as ft
from flet.controls.base_control import BaseControl

PAGE_ID = 1


class ErrorCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


_server = None
_port = None
errors = ErrorCollector()


def start_server() -> int:
    global _server, _port
    if _port:
        return _port
    from teampath.web.main import app

    for name in ("flet", "flet_web", "teampath", "flet_controls", "flet_transport"):
        logging.getLogger(name).addHandler(errors)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    _port = s.getsockname()[1]
    s.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=_port, log_level="warning", ws="websockets")
    _server = uvicorn.Server(config)
    threading.Thread(target=_server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{_port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    return _port


def _walk(obj, seen=None):
    if seen is None:
        seen = set()
    if isinstance(obj, BaseControl):
        if id(obj) in seen:
            return
        seen.add(id(obj))
        yield obj
        for f in dataclasses.fields(obj):
            if f.name.startswith("on_") or f.name in ("parent", "_parent", "page"):
                continue
            try:
                v = getattr(obj, f.name)
            except Exception:
                continue
            yield from _walk(v, seen)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v, seen)


def text_of(c) -> str:
    """Visible text of a control (label/text/value/content text)."""
    parts = []
    for attr in ("value", "label", "text", "tooltip", "title", "hint_text"):
        v = getattr(c, attr, None)
        if isinstance(v, str):
            parts.append(v)
    for attr in ("content", "title", "label"):
        v = getattr(c, attr, None)
        if isinstance(v, str) and attr == "content":
            parts.append(v)
        elif isinstance(v, ft.Text) and isinstance(v.value, str):
            parts.append(v.value)
    return " ".join(parts)


class UIClient:
    def __init__(self, width=1280, height=900):
        self.width, self.height = width, height
        self.ws = None
        self.route = "/"
        self.prefs: dict[str, Any] = {}
        self.picked_files: list[tuple[str, bytes]] = []
        self.upload_queue: list[tuple[int, str, str]] = []
        self.crash: str | None = None
        self.last_msg = time.time()
        self.launched_urls: list[str] = []
        self._reader = None
        self.page = None

    # ------------------------------------------------------------ connection
    async def connect(self, route="/"):
        port = start_server()
        self.base = f"http://127.0.0.1:{port}"
        self.ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None)
        self.route = route
        before = set(_state_ids())
        await self._send(1, {"session_id": "", "page_name": "", "page": {
            "route": route, "width": self.width, "height": self.height, "web": True, "platform": "linux",
            "media": {"padding": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                      "view_padding": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                      "view_insets": {"left": 0, "top": 0, "right": 0, "bottom": 0}, "device_pixel_ratio": 1.0}}})
        self._reader = asyncio.create_task(self._read())
        for _ in range(100):
            new = set(_state_ids()) - before
            if new:
                from teampath.ui.state import _STATES

                self.state = _STATES[new.pop()]
                self.page = self.state.page
                break
            await asyncio.sleep(0.05)
        await self.settle()
        return self

    async def close(self):
        if self.ws:
            await self.ws.close()
        if self._reader:
            self._reader.cancel()

    async def _send(self, action: int, body: dict):
        await self.ws.send(b"\x00" + msgpack.packb([action, body], default=str))

    async def _read(self):
        try:
            async for data in self.ws:
                self.last_msg = time.time()
                if not data or data[0] != 0:
                    continue
                action, body = msgpack.unpackb(data[1:], strict_map_key=False, raw=False)
                if action == 5:
                    asyncio.create_task(self._invoke(body))
                elif action == 6:
                    self.crash = str(body)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    async def _invoke(self, body):
        name, args, cid, call_id = body["name"], body.get("args") or {}, body["control_id"], body["call_id"]
        result = None
        if name == "push_route":
            asyncio.create_task(self._route_change(args["route"]))
        elif name == "set":
            self.prefs[args["key"]] = args["value"]
            result = True
        elif name == "get":
            result = self.prefs.get(args["key"])
        elif name in ("remove",):
            self.prefs.pop(args["key"], None)
            result = True
        elif name == "contains_key":
            result = args["key"] in self.prefs
        elif name == "pick_files":
            result = [{"id": i, "name": n, "size": len(b), "path": None, "bytes": None}
                      for i, (n, b) in enumerate(self.picked_files)]
        elif name == "upload":
            for f in args["files"]:
                asyncio.create_task(self._do_upload(cid, f))
        elif name in ("launch_url", "open_window"):
            self.launched_urls.append(args.get("url"))
            result = True
        await self._send(5, {"control_id": cid, "call_id": call_id, "result": result, "error": None})

    async def _do_upload(self, picker_id, f):
        name, data = self.picked_files[f["id"]]
        url = f["upload_url"]
        async with httpx.AsyncClient(base_url=self.base) as client:
            r = await client.put("/" + url.lstrip("/"), content=data)
        if r.status_code != 200:
            await self._send(3, {"target": picker_id, "name": "upload",
                                 "data": {"file_name": name, "progress": None, "error": f"HTTP {r.status_code}"}})
            return
        await self._send(3, {"target": picker_id, "name": "upload", "data": {"file_name": name, "progress": 1.0}})

    async def _route_change(self, route):
        self.route = route
        await self._send(4, {"id": PAGE_ID, "props": {"route": route}})
        await self._send(3, {"target": PAGE_ID, "name": "route_change", "data": {"route": route}})

    # ------------------------------------------------------------ driving the UI
    async def settle(self, quiet=0.3, timeout=20):
        from teampath.ui import state

        start = time.time()
        await asyncio.sleep(0.1)
        while time.time() - start < timeout:
            if state.INFLIGHT == 0 and time.time() - self.last_msg > quiet:
                return
            await asyncio.sleep(0.05)

    async def goto(self, route):
        await self._route_change(route)
        await self.settle()

    def controls(self):
        roots = [self.page.controls, self.page._dialogs.controls]
        return list(_walk(roots))

    def dialogs(self):
        return [d for d in self.page._dialogs.controls if getattr(d, "open", False) and not isinstance(d, ft.SnackBar)]

    def texts(self) -> str:
        return "\n".join(t for t in (text_of(c) for c in self.controls()) if t)

    def find(self, pred: Callable[[Any], bool], kind=None, in_dialog=False):
        roots = [self.dialogs()[-1]] if in_dialog and self.dialogs() else [self.page.controls, self.page._dialogs.controls]
        for c in _walk(roots):
            if (kind is None or isinstance(c, kind)) and pred(c):
                return c
        return None

    def button(self, label: str, in_dialog=False):
        kinds = (ft.FilledButton, ft.TextButton, ft.OutlinedButton, ft.Button, ft.IconButton, ft.FilledTonalButton,
                 ft.Chip, ft.ListTile, ft.PopupMenuItem, ft.Container)
        c = self.find(lambda c: label in text_of(c) and getattr(c, "on_click", None) is not None, kinds, in_dialog)
        if not c:
            raise AssertionError(f"No clickable '{label}'.\nVisible text:\n{self.texts()[:3000]}")
        return c

    def field(self, label: str, in_dialog=False):
        c = self.find(lambda c: getattr(c, "label", None) == label, (ft.TextField, ft.Dropdown), in_dialog)
        if not c:
            raise AssertionError(f"No field '{label}'.\nVisible text:\n{self.texts()[:3000]}")
        return c

    async def fire(self, control, name="click", data=None):
        assert control._i, f"control not mounted: {control}"
        await self._send(3, {"target": control._i, "name": name, "data": data})
        await self.settle()
        self.assert_ok()

    async def click(self, label: str, in_dialog=False):
        await self.fire(self.button(label, in_dialog))

    async def fill(self, label: str, value: str, in_dialog=False):
        f = self.field(label, in_dialog)
        await self._send(4, {"id": f._i, "props": {"value": value}})
        await asyncio.sleep(0.05)
        if isinstance(f, ft.Dropdown):
            await self._send(3, {"target": f._i, "name": "select", "data": value})
            await self.settle()

    async def set_value(self, control, value, event="change"):
        await self._send(4, {"id": control._i, "props": {"value": value}})
        await asyncio.sleep(0.05)
        if event:
            await self._send(3, {"target": control._i, "name": event, "data": None})
        await self.settle()
        self.assert_ok()

    async def segment(self, value: str, in_dialog: bool = False):
        sb = self.find(lambda c: any(getattr(seg, "value", None) == value for seg in getattr(c, "segments", []) or []),
                       ft.SegmentedButton, in_dialog)
        assert sb, f"No segment '{value}'"
        await self._send(4, {"id": sb._i, "props": {"selected": [value]}})
        await asyncio.sleep(0.05)
        await self._send(3, {"target": sb._i, "name": "change", "data": [value]})
        await self.settle()
        self.assert_ok()

    def assert_ok(self):
        if self.crash:
            raise AssertionError(f"session crashed: {self.crash}")
        if errors.records:
            recs = errors.records[:]
            errors.records.clear()
            msgs = []
            for r in recs:
                msgs.append(r.getMessage() + ("\n" + logging.Formatter().formatException(r.exc_info) if r.exc_info else ""))
            raise AssertionError("Server errors:\n" + "\n---\n".join(msgs))

    def has_text(self, s: str) -> bool:
        return s in self.texts()


def _state_ids():
    from teampath.ui.state import _STATES

    return list(_STATES.keys())
