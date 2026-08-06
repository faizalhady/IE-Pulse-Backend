"""
mes_webapi.py  (cycle_time)
───────────────────────────
Thin client for the MES JEMS Web API (mypenm0soap03/meswebapi).

Every call is POST with a JSON [FromBody] DTO + an `APIKey` header. This is a
SEPARATE system from the eBuild buildplan (which uses MES_SQL_* / pyodbc). Used
by the completion-status feature to resolve a model's MES Assembly_ID and fetch
its configured route steps.

Rules (from docs/MES/MESWebApi_REFERENCE.md):
  - Send header `APIKey` on every call.
  - NEVER send sqlServer / dataBase — even blank makes MES connect to a dead host.
  - Read-only endpoints only here; ⚠️ mutating endpoints are out of scope.
"""

import logging
import time

import requests

from modules.cycle_time.config import MES_WEBAPI_BASE, MES_WEBAPI_KEY, MES_WEBAPI_TIMEOUT

log = logging.getLogger(__name__)

# ─── TLS: use the Windows certificate store ──────────────────────────────────
# MES presents a cert issued by "CN=Jabil Enterprise KF ICA" — Jabil's internal
# CA. Windows trusts it (Postman and Invoke-WebRequest both work); Python does
# not, because `requests` ships its own bundle of ~150 PUBLIC CAs and a private
# corporate CA is never in it. Every call failed with:
#
#     SSLError: unable to get local issuer certificate
#
# Confirmed 2026-08-03 to be trust-only, not network or auth: the identical
# request with verify=False returned data. MES last answered on 28 Jul, and
# nothing changed client-side (certifi untouched since May) — so the server or
# a proxy stopped supplying the intermediate cert, which Windows can fill in
# from its own store and Python cannot.
#
# truststore makes Python validate against the Windows store, exactly like
# Postman. Verification stays ON — this is not verify=False — and it keeps
# working when IT rotates the CA.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception as e:                      # never block a call over this
    log.warning("truststore unavailable (%s) - TLS will use the bundled CA list, "
                "which does not include Jabil's internal CA", e)

_MAX_RETRIES = 3          # the MES SP intermittently 404s the same URL — retry helps
_BACKOFF_S = 2.0          # 2s, 4s, 6s


class MESWebApiError(RuntimeError):
    """Any failure talking to the MES Web API (missing key, HTTP error, bad body)."""


def post(controller: str, method: str, body: dict) -> list[dict]:
    """POST /meswebapi/<controller>/<method> and return the rows as a list of dicts.

    Retries transient errors (the MES SP will 404/5xx/timeout the *same* URL
    intermittently). MES returns either a bare JSON array or an object wrapping
    the array — both are normalised to a list.
    """
    if not MES_WEBAPI_KEY:
        raise MESWebApiError("MES_WEBAPI_KEY not set — add the MES Web API APIKey to .env")

    url = f"{MES_WEBAPI_BASE}/{controller}/{method}"
    body = _clean_ids(body)
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=body,
                              headers={"APIKey": MES_WEBAPI_KEY, "Accept": "application/json"},
                              timeout=MES_WEBAPI_TIMEOUT)
            r.raise_for_status()
            return _rows(r.json())
        except ValueError as e:  # bad JSON — not retriable
            raise MESWebApiError(f"{controller}/{method} returned non-JSON: {e}") from e
        except requests.RequestException as e:
            # 500 here is a permanent MES ApplicationException (e.g. "Invalid fmaRoute"
            # from a blank param — pass '%' not '') — retrying wastes time. Only the
            # SP's intermittent 404 / gateway / timeout is worth a retry.
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code not in (404, 429, 502, 503, 504) and not isinstance(e, (requests.Timeout, requests.ConnectionError)):
                raise MESWebApiError(f"{controller}/{method} failed: {_err_detail(e)}") from e
            last_err = e
            if attempt < _MAX_RETRIES:
                log.warning("  %s/%s attempt %d/%d failed (%s) - retrying",
                            controller, method, attempt, _MAX_RETRIES, e)
                time.sleep(_BACKOFF_S * attempt)
    raise MESWebApiError(f"{controller}/{method} failed after {_MAX_RETRIES} tries: {last_err}") from last_err


# Every id-ish param MES exposes. A float here is fatal: MES string-compares the
# value, so "110.0" raises 'Invalid custId' while "110" returns 1,086 rows. pandas
# hands ids back as float64 whenever a column has one NaN, so this WILL happen —
# it cost the 2026-07-24 run 3 hours and 38/38 customers. Guard it centrally so no
# call site has to remember. Proven: '110'/110/' 110 ' all work, '110.0' fails.
_ID_KEYS = {"custid", "customerid", "assemid", "fmarouteid", "cid", "customer_id"}


def _clean_ids(body: dict) -> dict:
    """Coerce id params to a bare integer string: 110.0 / '110.0' / 110 -> '110'."""
    if not isinstance(body, dict):
        return body
    out = {}
    for k, v in body.items():
        if str(k).lower() in _ID_KEYS and v not in (None, ""):
            try:
                out[k] = str(int(float(str(v).strip())))
                continue
            except (TypeError, ValueError):
                pass          # not numeric (some ids are codes) — pass through untouched
        out[k] = v
    return out


def _err_detail(e: Exception) -> str:
    """Pull MES's ExceptionMessage from a failed response when present — the SP
    reports the real cause there (e.g. 'Invalid fmaRoute')."""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            j = resp.json()
            return j.get("ExceptionMessage") or j.get("Message") or str(e)
        except ValueError:
            pass
    return str(e)


def _rows(data) -> list[dict]:
    """Normalise a MES response to list[dict]. Handles: a bare list; a dict that
    wraps the list under some key; or a single-object dict."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
        return [data]
    return []


if __name__ == "__main__":
    # Offline self-check of the response-shape normaliser (no network / key needed).
    assert _rows([{"a": 1}]) == [{"a": 1}]
    assert _rows({"rows": [{"a": 1}, {"a": 2}]}) == [{"a": 1}, {"a": 2}]
    assert _rows({"Customer_ID": 5, "Customer": "X"}) == [{"Customer_ID": 5, "Customer": "X"}]
    assert _rows(None) == []

    # id normaliser — the 2026-07-24 root cause. Every form must reach MES as "110".
    for v in (110, 110.0, "110", "110.0", " 110 "):
        assert _clean_ids({"custId": v})["custId"] == "110", f"custId {v!r} not normalised"
    assert _clean_ids({"CustomerID": 7.0})["CustomerID"] == "7"
    assert _clean_ids({"serial": "2624201927.0"})["serial"] == "2624201927.0"  # not an id key
    assert _clean_ids({"custId": ""})["custId"] == ""                          # blank passes through
    assert _clean_ids({"custId": "ABC"})["custId"] == "ABC"                    # non-numeric id
    print("mes_webapi self-check OK (_rows + _clean_ids)")
