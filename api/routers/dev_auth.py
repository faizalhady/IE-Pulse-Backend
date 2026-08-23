"""
api/routers/dev_auth.py
───────────────────────
Dev sign-in. In production the browser gets its token from AD_GET (Windows auth on
mypenm0iesvr02:5110). From a developer laptop off the Jabil network that name does not
even resolve, so every API call is a 401 and the dev tools fill with red.

  GET /api/dev/token  ->  {ntid, token, fullName}   a token signed with PULSE_JWT_SECRET,
                                                    exactly what AD_GET would have minted

Exists only when PULSE_DEV_NTID is set in .env (it never is on 02) and only answers a
caller on the loopback interface. The frontend uses it only in DEV builds, only after
AD_GET failed (see src/hooks/useCurrentUser.ts).
"""

from __future__ import annotations

import os
import time

import jwt
from fastapi import APIRouter, HTTPException, Request

from core.auth import ALGORITHM, ISSUER, _secret

router = APIRouter(prefix="/api/dev", tags=["dev"])

TOKEN_HOURS = 8


@router.get("/token")
def dev_token(request: Request):
    ntid = os.getenv("PULSE_DEV_NTID", "").strip()
    if not ntid:
        raise HTTPException(404, "Not found")
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(404, "Not found")
    now = int(time.time())
    token = jwt.encode({"sub": ntid, "iss": ISSUER, "iat": now, "exp": now + TOKEN_HOURS * 3600}, _secret(), algorithm=ALGORITHM)
    return {"ntid": ntid, "token": token, "fullName": f"Dev ({ntid})", "expires_in": TOKEN_HOURS * 3600}
