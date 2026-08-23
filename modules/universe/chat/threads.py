"""
modules/universe/chat/threads.py
────────────────────────────────
Saved chats, per user, in the operational SQLite (user-entered data lives there by
rule; tables in core/database.py::init_db). A thread is visible only to its owner; messages keep their UIMessage parts so a
reopened chat renders exactly as it streamed; feedback sits on the message.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from core.database import get_conn

TITLE_CHARS = 60

# tables: core/database.py::init_db (chat_thread, chat_message)


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _title(question: str) -> str:
    return " ".join((question or "").split())[:TITLE_CHARS] or "New chat"


def create(ntid: str, first_question: str) -> dict:
    row = {"id": uuid.uuid4().hex, "ntid": ntid.lower(), "title": _title(first_question), "created_at": _now(), "updated_at": _now()}
    with get_conn() as conn:
        conn.execute("INSERT INTO chat_thread (id, ntid, title, created_at, updated_at) VALUES (:id, :ntid, :title, :created_at, :updated_at)", row)
    return row


def add_message(thread_id: str, role: str, parts: list[dict], model: str | None = None, message_id: str | None = None) -> dict:
    """message_id: the id the stream announced in its `start` chunk, so the page's thumbs
    find the saved answer under the same id the client already holds."""
    row = {"id": message_id or uuid.uuid4().hex, "thread_id": thread_id, "role": role, "parts": json.dumps(parts, default=str),
           "model": model, "created_at": _now()}
    with get_conn() as conn:
        conn.execute("INSERT INTO chat_message (id, thread_id, role, parts, model, created_at) VALUES (:id, :thread_id, :role, :parts, :model, :created_at)", row)
        conn.execute("UPDATE chat_thread SET updated_at = ? WHERE id = ?", (row["created_at"], thread_id))
    return {"id": row["id"], "role": role, "parts": parts, "model": model, "created_at": row["created_at"]}


def list_for(ntid: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, title, created_at, updated_at FROM chat_thread WHERE ntid = ? ORDER BY updated_at DESC",
                            (ntid.lower(),)).fetchall()
    return [dict(r) for r in rows]


def get(thread_id: str, ntid: str) -> dict | None:
    with get_conn() as conn:
        t = conn.execute("SELECT id, title, created_at, updated_at FROM chat_thread WHERE id = ? AND ntid = ?",
                         (thread_id, ntid.lower())).fetchone()
        if t is None:
            return None
        msgs = conn.execute("SELECT id, role, parts, model, created_at, feedback, feedback_reason FROM chat_message WHERE thread_id = ? ORDER BY created_at, rowid",
                            (thread_id,)).fetchall()
    out = dict(t)
    out["messages"] = [{**dict(m), "parts": json.loads(m["parts"])} for m in msgs]
    return out


def rename(thread_id: str, ntid: str, title: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE chat_thread SET title = ?, updated_at = ? WHERE id = ? AND ntid = ?",
                           (_title(title), _now(), thread_id, ntid.lower()))
    return cur.rowcount > 0


def delete(thread_id: str, ntid: str) -> bool:
    with get_conn() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM chat_message WHERE thread_id = ? AND thread_id IN (SELECT id FROM chat_thread WHERE id = ? AND ntid = ?)",
                     (thread_id, thread_id, ntid.lower()))
        cur = conn.execute("DELETE FROM chat_thread WHERE id = ? AND ntid = ?", (thread_id, ntid.lower()))
    return cur.rowcount > 0


def feedback(message_id: str, ntid: str, vote: int, reason: str | None) -> bool:
    """vote: 1 (up) · -1 (down) · 0 (clear). Only the thread's owner may vote."""
    with get_conn() as conn:
        cur = conn.execute("""UPDATE chat_message SET feedback = ?, feedback_reason = ?
                              WHERE id = ? AND thread_id IN (SELECT id FROM chat_thread WHERE ntid = ?)""",
                           (vote or None, (reason or "").strip() or None, message_id, ntid.lower()))
    return cur.rowcount > 0
