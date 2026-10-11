"""Event bus used to push live updates to open Flet sessions.

Services publish small events (ids only, never sensitive content). Each UI
session subscribes and re-reads data through its own tenant context, so RLS
still decides what it can see.

The web app and the worker are separate processes (separate containers), so
events published by the worker travel through Postgres LISTEN/NOTIFY: the
worker calls `use_postgres_notify()` at start-up, and the web app runs
`start_listener()`, which re-publishes every notification locally.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from typing import Callable

log = logging.getLogger(__name__)
CHANNEL = "teampath_events"
JOBS_CHANNEL = "teampath_jobs"

_lock = threading.Lock()
_subs: dict[str, dict[int, Callable[[dict], None]]] = defaultdict(dict)
_mode = "local"
_listener: threading.Thread | None = None


def subscribe(topic: str, key: int, fn: Callable[[dict], None]) -> None:
    with _lock:
        _subs[topic][key] = fn


def unsubscribe_all(key: int) -> None:
    with _lock:
        for subs in _subs.values():
            subs.pop(key, None)


def publish_local(topic: str, **event) -> None:
    with _lock:
        targets = list(_subs.get(topic, {}).values())
    for fn in targets:
        try:
            fn(event)
        except Exception:  # a broken session must not break the publisher
            log.exception("event handler failed")


def publish(topic: str, **event) -> None:
    if _mode == "notify":
        _notify(topic, event)
    else:
        publish_local(topic, **event)


def use_postgres_notify() -> None:
    """Called by processes without UI sessions (the worker)."""
    global _mode
    _mode = "notify"


def _notify(topic: str, event: dict) -> None:
    from sqlalchemy import text

    from .db import system_session

    payload = json.dumps({"topic": topic, "event": event}, default=str)
    try:
        with system_session("events: notify web app") as db:
            db.execute(text("select pg_notify(:c, :p)"), {"c": CHANNEL, "p": payload})
    except Exception:
        log.exception("could not send event")


def _raw_dsn() -> str:
    from .config import get_settings

    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def start_listener() -> None:
    """Web app: forward Postgres notifications to local subscribers (background thread)."""
    global _listener
    if _listener and _listener.is_alive():
        return

    def run():
        import psycopg

        while True:
            try:
                with psycopg.connect(_raw_dsn(), autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    log.info("listening for worker events")
                    for n in conn.notifies():
                        try:
                            msg = json.loads(n.payload)
                            publish_local(msg["topic"], **msg.get("event", {}))
                        except Exception:
                            log.exception("bad event payload")
            except Exception:
                log.warning("event listener disconnected; reconnecting", exc_info=True)
                time.sleep(3)

    _listener = threading.Thread(target=run, name="event-listener", daemon=True)
    _listener.start()


def org_topic(org_id) -> str:
    return f"org:{org_id}"


def user_topic(user_id) -> str:
    return f"user:{user_id}"
