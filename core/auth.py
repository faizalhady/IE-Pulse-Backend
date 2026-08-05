"""
core/auth.py
────────────
Who is calling. Not what they may do — that is `user_access` (api/routers/access.py).

BACKGROUND
This service runs as LocalSystem behind nginx and never sees a Windows identity:
NTLM is bound to the TCP connection, so it cannot be forwarded to uvicorn. Until
now the NTID arrived as a plain request value, which anyone on the intranet could
forge. AD_GET is the only process that authenticates the user, so it signs a
short-lived JWT and we verify it here.

VERIFICATION USES PyJWT ON PURPOSE
Signing is easy; verifying is where JWT goes wrong (alg=none, algorithm
confusion, unchecked expiry, non-constant-time comparison). PyJWT handles those.
`algorithms=["HS256"]` is pinned so a token claiming a different alg is rejected
outright rather than validated under attacker-chosen rules.

FAIL CLOSED
If PULSE_JWT_SECRET is unset the dependency raises 503 rather than waving callers
through. A missing secret is a deployment fault, and "auth silently disabled" is
exactly the failure that goes unnoticed for months.
"""

import logging
import os
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status

log = logging.getLogger(__name__)

ALGORITHM = "HS256"
ISSUER = "ad-get"


def _secret() -> str:
    s = os.getenv("PULSE_JWT_SECRET", "").strip()
    if not s:
        # Loud: this is a misconfiguration, not a user error.
        log.error("PULSE_JWT_SECRET is not set — identity cannot be verified.")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Identity verification is not configured on this server.",
        )
    return s


def verified_ntid(authorization: Optional[str] = Header(None)) -> str:
    """The caller's NTID, proven by AD_GET's signature.

    Use as a dependency on anything that writes, or that reveals one person's
    data to another:

        @router.put("/{ntid}")
        def upsert(ntid: str, caller: str = Depends(verified_ntid)):
            ...

    Raises 401 for a missing, malformed, expired or wrongly-signed token. The
    error text stays vague on purpose — telling a caller *why* their forged token
    failed just helps them forge a better one.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign-in required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=[ALGORITHM],   # pinned — never read the alg from the token
            issuer=ISSUER,
            options={"require": ["sub", "exp", "iss"]},
        )
    except HTTPException:
        raise
    except jwt.InvalidTokenError as e:
        log.warning("rejected token: %s: %s", type(e).__name__, e)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign-in expired or invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    ntid = str(claims.get("sub") or "").strip()
    if not ntid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in required.")
    return ntid


RANK = {"viewer": 0, "admin": 1, "super_admin": 2, "developer": 3}


def optional_ntid(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """The caller's NTID if they proved one, else None — never raises.

    For endpoints that must answer anonymous callers rather than reject them,
    e.g. the UI asking "may I edit?" before it knows who it is.
    """
    try:
        return verified_ntid(authorization)
    except HTTPException:
        return None


def level_of(ntid: Optional[str]) -> str:
    """Their level in user_access. Absent means viewer — consistent with
    /api/access/me, where never having been granted anything is not an error."""
    from core.database import get_conn

    if not ntid:
        return "viewer"
    with get_conn() as conn:
        row = conn.execute(
            "SELECT level FROM user_access WHERE lower(ntid) = lower(?)", (ntid,)
        ).fetchone()
    return row["level"] if row else "viewer"


def require_level(minimum: str):
    """Dependency factory: caller must be at least `minimum`.

    Reads the level from user_access at request time rather than from the token,
    so revoking someone takes effect immediately instead of whenever their token
    happens to expire.
    """
    if minimum not in RANK:
        raise ValueError(f"unknown level {minimum!r}")

    def dep(ntid: str = Depends(verified_ntid)) -> str:
        level = level_of(ntid)
        if RANK[level] < RANK[minimum]:
            log.warning("denied %s (%s) — needs %s", ntid, level, minimum)
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This needs {minimum.replace('_', ' ')} access.",
            )
        return ntid

    return dep
