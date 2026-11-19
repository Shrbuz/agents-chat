"""Read-only activity of bound chats on the local Codex host."""
import threading
import time

from .codex_connection import select_connection
from .desktop_transport import CodexDesktop, ProbeBackoff


def read_activity(desktop, sessions, stop):
    """History pages have the host's sidebar status, including older bound chats."""
    states = {}
    args = {"mode": "history", "source": "codex", "hostId": "local", "limit": 50}
    cursors = set()
    while not stop.is_set():
        page = desktop.call("list_threads", args, timeout=5)
        for item in page["threads"]:
            session = item.get("id")
            if session in sessions and item.get("hostId") == "local":
                status = item.get("status")
                if isinstance(status, dict):
                    status = status.get("type")
                states[session] = status if status in ("active", "idle", "notLoaded") else "unknown"
        cursor = page.get("nextCursor")
        if not cursor or cursor in cursors or sessions.issubset(states):
            break
        cursors.add(cursor)
        args["cursor"] = cursor
    return states


class CodexActivity:
    def __init__(self, hub):
        self.hub = hub
        # Replace the entire observation at once; readers never wait on desktop I/O.
        self.latest = (None, 0.0, {})

    def snapshot(self, generation):
        observed_generation, observed_at, states = self.latest
        if generation and generation == observed_generation and time.monotonic() - observed_at < 12:
            return states
        return {}

    def run(self, stop):
        desktop = generation = None
        backoff = ProbeBackoff()
        try:
            while not stop.is_set():
                delay = 3
                try:
                    with self.hub.cv:
                        sessions = {b["session_id"] for b in self.hub.bindings.values()
                                    if b.get("transport") == "codex_app_tools" and b.get("session_id")}
                    connection = select_connection(self.hub.state_dir) if sessions else None
                    current = connection["connection_id"] if connection else None
                    if desktop and current != generation:
                        desktop.close()
                        desktop = None
                    generation = current
                    if connection:
                        if desktop is None:
                            desktop = CodexDesktop(connection)
                        states = read_activity(desktop, sessions, stop)
                        self.latest = (generation, time.monotonic(), states)
                        backoff.reset()
                    else:
                        self.latest = (None, 0.0, {})
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    self.latest = (None, 0.0, {})
                    if desktop:
                        desktop.close()
                        desktop = None
                    delay = backoff.failed()
                stop.wait(delay)
        finally:
            if desktop:
                desktop.close()
