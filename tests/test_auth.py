"""
Self-check for core/auth.py — the forgery cases specifically.

Run: venv/Scripts/python.exe -m tests.test_auth

Deliberately not a pytest suite: this exists so that if someone later "simplifies"
the verification, the ways a token can be faked get caught rather than discovered.
"""

import base64
import hashlib
import hmac
import json
import os
import time

SECRET = "test-secret-not-the-real-one"
os.environ["PULSE_JWT_SECRET"] = SECRET

from fastapi import HTTPException  # noqa: E402

from core.auth import ISSUER, verified_ntid  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make(secret=SECRET, alg="HS256", sub="4033375", iss=ISSUER, exp_delta=3600, sign=True):
    """Mirrors AD_GET's IdentityToken.Sign so the two stay honest about the format."""
    now = int(time.time())
    header = _b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(
        {"sub": sub, "iss": iss, "iat": now, "exp": now + exp_delta}).encode())
    body = f"{header}.{payload}"
    if not sign:
        return f"{body}."
    sig = _b64(hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def rejects(token, why):
    try:
        verified_ntid(f"Bearer {token}" if token is not None else None)
    except HTTPException as e:
        assert e.status_code in (401, 503), f"{why}: wrong status {e.status_code}"
        return
    raise AssertionError(f"ACCEPTED a token it must reject: {why}")


def demo():
    # The one case that must work.
    assert verified_ntid(f"Bearer {make()}") == "4033375"
    # Case-preserving, non-numeric NTIDs (LawC2, LoghanaA) survive the round trip.
    assert verified_ntid(f"Bearer {make(sub='LawC2')}") == "LawC2"

    rejects(None, "no header at all")
    rejects("", "empty token")
    rejects("garbage", "not a JWT")
    rejects(make(secret="wrong-secret"), "signed with the wrong secret")
    rejects(make(sign=False), "no signature at all")
    rejects(make(alg="none", sign=False), "alg=none downgrade")
    rejects(make(exp_delta=-60), "expired an hour ago")
    rejects(make(iss="somebody-else"), "issued by someone other than AD_GET")
    rejects(make(sub=""), "empty subject")

    # A missing secret must fail closed (503), never silently allow.
    os.environ["PULSE_JWT_SECRET"] = ""
    rejects(make(), "server has no secret configured")
    os.environ["PULSE_JWT_SECRET"] = SECRET

    print("  auth self-check passed (1 accept, 10 rejects)")


if __name__ == "__main__":
    demo()
