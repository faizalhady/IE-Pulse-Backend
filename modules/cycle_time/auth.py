"""
modules/cycle_time/auth.py
──────────────────────────
IEDB3.0 OAuth 2.0 token manager (client_credentials flow).

Ported from the previous .NET service (iedb_auth_service.cs):
  POST {TOKEN_URL}
       Authorization: Basic <IEDB_CLIENT_KEY>     ← base64(client_id:client_secret)
       Content-Type:  application/x-www-form-urlencoded
       Body:          grant_type=client_credentials&scope=access_token

Response → { access_token, expires_in, ... }

Tokens are cached in-process and refreshed automatically TOKEN_EXPIRY_BUFFER_S
seconds before expiry. A threading.Lock prevents the double-fetch race when
multiple background tasks request a token simultaneously.

  Usage:
      from modules.cycle_time.auth import get_token
      token = get_token()
"""

import logging
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from modules.cycle_time.config import API_TIMEOUT, TOKEN_EXPIRY_BUFFER_S, TOKEN_URL

# Anchor the .env to the repo root, NOT the process CWD. A bare load_dotenv()
# searches upward from the working directory — which under a Windows service
# (Servy) is not the backend folder, so .env was never found and
# IEDB_CLIENT_KEY read empty even though the file is right here. Resolving an
# absolute path makes the key load regardless of how/where the process is launched.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_PATH)
log = logging.getLogger(__name__)
log.info("IEDB auth: .env path %s (exists=%s, key_loaded=%s)",
         _ENV_PATH, _ENV_PATH.exists(), bool(os.getenv("IEDB_CLIENT_KEY", "").strip()))


class _TokenCache:
    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0  # unix epoch seconds
        self._lock = threading.Lock()

    def get(self) -> str:
        # Fast path — no lock needed if the cached token is comfortably valid.
        if self._fresh():
            return self._token  # type: ignore[return-value]
        # Slow path — serialise refreshes so we don't hit the token endpoint N times.
        with self._lock:
            if self._fresh():
                return self._token  # type: ignore[return-value]
            return self._refresh()

    def _fresh(self) -> bool:
        return (
            self._token is not None
            and time.time() < self._expires_at - TOKEN_EXPIRY_BUFFER_S
        )

    def _refresh(self) -> str:
        key = os.getenv("IEDB_CLIENT_KEY", "").strip()
        if not key:
            raise EnvironmentError(
                "IEDB_CLIENT_KEY is not set.\n"
                f"Looked for it in: {_ENV_PATH} (exists={_ENV_PATH.exists()}).\n"
                "Add it to that .env file. This is the base64 of "
                "<client_id>:<client_secret> from Okta — same value the "
                "previous .NET service used under IEDB:Key."
            )

        log.info("Fetching new IEDB access token ...")
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {key}",
                "Accept":         "application/json",
                "Cache-Control":  "no-cache",
            },
            data={
                "grant_type": "client_credentials",
                "scope":      "access_token",
            },
            timeout=API_TIMEOUT,
        )

        if resp.status_code == 401:
            raise PermissionError(
                "401 from Okta token endpoint — IEDB_CLIENT_KEY is rejected. "
                "Verify it matches the IEDB:Key the .NET service uses."
            )
        resp.raise_for_status()

        body = resp.json()
        token = body.get("access_token")
        expires_in = int(body.get("expires_in", 3600))
        if not token:
            raise RuntimeError(f"Okta token response missing access_token: {body}")

        self._token = token
        self._expires_at = time.time() + expires_in
        log.info(f"IEDB token cached; expires in {expires_in}s (~{expires_in // 60} min)")
        return token


_cache = _TokenCache()


def get_token() -> str:
    """Return a valid IEDB Bearer token, refreshing if needed."""
    return _cache.get()


def invalidate() -> None:
    """Force the next get_token() call to refetch. Use after a 401 from IEDB
    in case the cached token was revoked early."""
    _cache._token = None
    _cache._expires_at = 0.0
