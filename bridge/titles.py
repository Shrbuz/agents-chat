"""Best-effort chat titles from a client's own local store.

The Qoder and Codex connectors do not expose a chat title over their protocol,
but Qoder keeps one in its SQLite database. Reading it is a read-only lookup of
the same row it already uses, so a binding can be labelled with the name the
user sees in the app.

Every lookup is best effort: a missing file, a locked database or an unknown
session simply falls back to the caller's default label.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


def _qoder_databases():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    root = Path(appdata) / "com.qoder.app.stable"
    return [root / "main.sqlite"]


def qoder_title(session_id):
    """Title Qoder shows for one conversation, or None."""
    if not session_id:
        return None
    for database in _qoder_databases():
        if not database.is_file():
            continue
        try:
            connection = sqlite3.connect("file:%s?mode=ro" % database.as_posix(), uri=True, timeout=2)
            try:
                row = connection.execute(
                    "SELECT title FROM chat_sessions WHERE session_id=?", (str(session_id),)).fetchone()
            finally:
                connection.close()
        except (sqlite3.Error, OSError):
            continue
        if row and row[0]:
            title = str(row[0]).strip()
            if title:
                return title
    return None


def dsh_title(session_id):
    """Title DSH shows for one session, from its project cache entry."""
    if not session_id:
        return None
    cache = Path(os.environ.get("USERPROFILE") or Path.home()) / ".dsh/storages/session_projcache/sessions"
    entry = cache / (str(session_id) + ".json")
    if not entry.is_file():
        return None
    try:
        data = json.loads(entry.read_text(encoding="utf-8"))
        title = (((data.get("record") or {}).get("rows") or {}).get("title") or {}).get("val")
    except (OSError, ValueError):
        return None
    return str(title).strip() if title else None


def desktop_title(transport, session_id):
    """Human name for a desktop-bound session, when the client store has one."""
    if transport == "qoder_native":
        return qoder_title(session_id)
    if transport == "opencode_native" and session_id:
        return str(session_id)
    if transport == "qwen_native" and session_id:
        return str(session_id)
    if transport == "kimi_native" and session_id:
        return str(session_id)
    if transport == "dsh_plugin" and session_id:
        # DSH keeps the chat title in its session cache; the session id is the
        # fallback when that entry is missing.
        return dsh_title(session_id) or str(session_id)
    return None


def desktop_label(transport, session_id, fallback_role):
    """Preferred label: the real chat title, else '<role> · <client>'."""
    return desktop_title(transport, session_id) or "%s · %s" % (fallback_role, transport or "desktop")

