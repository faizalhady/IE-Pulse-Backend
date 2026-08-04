"""
api/routers/access.py
─────────────────────
Who can do what, what they lead, and what they get emailed about.

One row per PERSON, not one per grant. A person has:
    level      viewer | admin | super_admin | developer
    apps       'all', or a CSV of module ids
    workcells  zero or many — leading one is a separate fact from level
    notify     opt-ins, keyed ('ole_smh' today)

Deliberately NOT a proxy to AD_GET. That service needs Windows auth and this
backend runs as LocalSystem, so it would authenticate as the machine account
rather than a person. The browser does NTLM for free in the intranet zone, so
the UI searches AD_GET directly and posts whoever it picked. Everything here
stores and serves what the UI already resolved.

Endpoints:
  GET    /api/access                users, with workcells + notifications
  GET    /api/access/me/{ntid}      one person's effective access
  GET    /api/access/recipients     who to email for a notification key
  PUT    /api/access/{ntid}         upsert a person (whole record — see below)
  DELETE /api/access/{ntid}         remove them entirely
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.database import get_conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/access", tags=["Access"])

Level = Literal["viewer", "admin", "super_admin", "developer"]

# Weakest to strongest — used for "is this person at least X?" checks.
_RANK = {"viewer": 0, "admin": 1, "super_admin": 2, "developer": 3}


class UserIn(BaseModel):
    """A person's WHOLE access record.

    PUT, not PATCH: the edit dialog shows every workcell and app at once, so it
    always knows the complete intended state. Sending the whole thing means
    un-ticking a workcell actually removes it — a partial update would need a
    separate 'remove' call and could silently leave orphans behind.
    """
    name: Optional[str] = Field(None, max_length=200)
    email: Optional[str] = Field(None, max_length=200)
    # From headcount — businessTitle and customer, carried at add time.
    position: Optional[str] = Field(None, max_length=200)
    customer: Optional[str] = Field(None, max_length=200)
    level: Level = "viewer"
    # 'all' rather than enumerating every module, so adding a new app does not
    # silently narrow everyone's existing access.
    apps: list[str] = Field(default_factory=lambda: ["all"])
    workcells: list[str] = Field(default_factory=list)
    primary_workcell: Optional[str] = None
    # key -> enabled, e.g. {"ole_smh": true}
    notifications: dict[str, bool] = Field(default_factory=dict)
    added_by: Optional[str] = Field(None, max_length=40)


def _load_users(conn, ntid: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM user_access"
    args: list = []
    if ntid:
        sql += " WHERE lower(ntid) = lower(?)"
        args.append(ntid)
    sql += (" ORDER BY CASE level WHEN 'developer' THEN 0 WHEN 'super_admin' THEN 1 "
            "WHEN 'admin' THEN 2 ELSE 3 END, name COLLATE NOCASE")
    users = [dict(r) for r in conn.execute(sql, args)]
    if not users:
        return []

    ids = [u["ntid"] for u in users]
    marks = ",".join("?" * len(ids))
    wc: dict[str, list] = {}
    for r in conn.execute(
            f"SELECT * FROM user_workcells WHERE ntid IN ({marks}) ORDER BY workcell", ids):
        wc.setdefault(r["ntid"], []).append({"workcell": r["workcell"], "is_primary": r["is_primary"]})
    nt: dict[str, dict] = {}
    for r in conn.execute(
            f"SELECT * FROM user_notifications WHERE ntid IN ({marks})", ids):
        nt.setdefault(r["ntid"], {})[r["key"]] = bool(r["enabled"])

    for u in users:
        u["apps"] = u["apps"].split(",") if u["apps"] else ["all"]
        u["workcells"] = [w["workcell"] for w in wc.get(u["ntid"], [])]
        u["primary_workcell"] = next(
            (w["workcell"] for w in wc.get(u["ntid"], []) if w["is_primary"]), None)
        u["notifications"] = nt.get(u["ntid"], {})
    return users


@router.get("")
def list_users():
    with get_conn() as conn:
        users = _load_users(conn)
    return {"count": len(users), "users": users}


@router.get("/me/{ntid}")
def effective_access(ntid: str):
    """What this person may do — the shape a permission check needs.

    `all_workcells` is true for super_admin and developer: enumerating every
    workcell for them would go stale the moment one is added, and "can edit
    anything" is the actual intent.
    """
    with get_conn() as conn:
        users = _load_users(conn, ntid)
    if not users:
        # Absence is not an error — everyone who was never granted anything is
        # a viewer. Returning 404 would make every caller special-case it.
        return {"ntid": ntid, "known": False, "level": "viewer", "apps": ["all"],
                "workcells": [], "all_workcells": False, "is_admin": False,
                "notifications": {}}
    u = users[0]
    return {
        "ntid": u["ntid"], "known": True, "level": u["level"], "apps": u["apps"],
        "workcells": u["workcells"],
        "all_workcells": _RANK[u["level"]] >= _RANK["super_admin"],
        "is_admin": _RANK[u["level"]] >= _RANK["admin"],
        "notifications": u["notifications"],
        "name": u["name"], "email": u["email"],
        "position": u["position"], "customer": u["customer"],
    }


@router.get("/recipients")
def recipients(key: str = Query("ole_smh", description="Notification key, e.g. 'ole_smh'.")):
    """Who to email for a notification, grouped by the workcell it concerns.

    People opted in but with no email in headcount come back under
    `unreachable` rather than being dropped — a missing recipient otherwise
    looks exactly like "nothing to report".
    """
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT ua.ntid, ua.name, ua.email, uw.workcell, uw.is_primary
            FROM user_notifications un
            JOIN user_access    ua ON ua.ntid = un.ntid
            JOIN user_workcells uw ON uw.ntid = un.ntid
            WHERE un.key = ? AND un.enabled = 1
            ORDER BY uw.workcell COLLATE NOCASE, uw.is_primary DESC
        """, (key,))]

    by_wc: dict[str, dict] = {}
    unreachable: list[dict] = []
    for r in rows:
        if not r["email"]:
            unreachable.append({"workcell": r["workcell"], "ntid": r["ntid"], "name": r["name"]})
            continue
        w = by_wc.setdefault(r["workcell"], {"workcell": r["workcell"], "to": [], "cc": []})
        # Primary in To, the rest Cc — one owner, everyone else informed.
        (w["to"] if r["is_primary"] else w["cc"]).append(
            {"ntid": r["ntid"], "name": r["name"], "email": r["email"]})

    for w in by_wc.values():
        if not w["to"] and w["cc"]:
            w["to"].append(w["cc"].pop(0))     # never leave To empty

    return {
        "key": key,
        "count": len(by_wc),
        "workcells": sorted(by_wc.values(), key=lambda x: x["workcell"].lower()),
        "unreachable": unreachable,
    }


@router.put("/{ntid}")
def upsert_user(ntid: str, body: UserIn):
    apps = ",".join(body.apps) if body.apps else "all"
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_access (ntid, name, email, position, customer, level, apps, added_by)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT (ntid) DO UPDATE SET
                name = excluded.name, email = excluded.email,
                position = excluded.position, customer = excluded.customer,
                level = excluded.level, apps = excluded.apps,
                updated_at = datetime('now')
        """, (ntid, body.name, body.email, body.position, body.customer,
              body.level, apps, body.added_by))

        # Replace rather than merge: the dialog always sends the full intended
        # set, so anything absent was deliberately un-ticked.
        conn.execute("DELETE FROM user_workcells WHERE ntid = ?", (ntid,))
        for w in body.workcells:
            conn.execute(
                "INSERT INTO user_workcells (ntid, workcell, is_primary) VALUES (?,?,?)",
                (ntid, w, int(w == body.primary_workcell)))

        conn.execute("DELETE FROM user_notifications WHERE ntid = ?", (ntid,))
        for k, on in body.notifications.items():
            if on:      # only store opt-INS; absence means off
                conn.execute(
                    "INSERT INTO user_notifications (ntid, key, enabled) VALUES (?,?,1)", (ntid, k))

        user = _load_users(conn, ntid)[0]

    log.info("access: %s = %s, apps=%s, workcells=%s", body.name or ntid,
             body.level, apps, body.workcells or "none")
    return user


@router.delete("/{ntid}")
def delete_user(ntid: str):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM user_access WHERE lower(ntid) = lower(?)", (ntid,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"No access record for {ntid}.")
        conn.execute("DELETE FROM user_workcells WHERE ntid = ?", (ntid,))
        conn.execute("DELETE FROM user_notifications WHERE ntid = ?", (ntid,))
    return {"deleted": ntid}
