"""
modules/ole/smh_email.py
────────────────────────
Emails each workcell's SMH owner the models they are still missing.

  python -m modules.ole.smh_email              # dry run — prints, sends nothing
  python -m modules.ole.smh_email --send       # actually sends

Not a separate service. Everything it needs already lives in this process:
the coverage mart, the live SMH table, the access DB that says who owns which
workcell, and the auth that gates the endpoint. A second process would need a
copy of all four and a way to keep them in sync.

Where each piece comes from:
  numbers    smh_assembly_status.parquet, overlaid with the live `smh` table
             (see below — a snapshot alone emails people about rows they fixed)
  owners     GET /api/access/recipients?key=ole_smh — reused, not re-queried,
             so To/Cc stays one rule
  link       /ietools/ole/smh?workcell=X&status=NOT_IN_SMH_DB — the SMH page
             already reads its filters from the URL, so this needs no new UI
"""

import argparse
import logging
import os
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

import pandas as pd
from dotenv import load_dotenv

from core.paths import PROJECT_ROOT
from modules.ole import smh_store
from modules.ole.config import MART

log = logging.getLogger(__name__)

# Anchored to the repo, not the CWD — api/main.py does the same. Without this the
# CLI silently falls back to the defaults below and mails from the wrong address.
load_dotenv(PROJECT_ROOT / ".env")

# Where the SMH page lives, for the deep links. Wrong value = dead links in
# everyone's inbox, so it is required rather than guessed at.
BASE_URL = os.getenv("PULSE_BASE_URL", "").rstrip("/")

# Anonymous relay on 25, no auth, no TLS — the same path eST1C, eCrate and
# IEPortal have used for years (SmtpClient with no Credentials). Two hosts, in
# order: this box first (the backend runs on it, so it is a loopback hop), then
# the corporate relay. Every one of those apps carries the same fallback pair,
# which is evidence the primary does go down.
SMTP_HOSTS = [h.strip() for h in os.getenv(
    "SMTP_HOST", "MYPENM0IESVR02.corp.JABIL.ORG,CORIMC04.corp.jabil.org"
).split(",") if h.strip()]
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
# Matches the house pattern: PEN_IEDigital@, PEN_EST1C@, FSMS_Notification@.
SMTP_FROM = os.getenv("SMTP_FROM", "PEN_IEPulse@jabil.com")

NOTIFY_KEY = "ole_smh"

# Who a recipient should contact — the sender mailbox is a relay, nobody reads
# replies to it. Env-overridable so this does not need a code change when the
# owner changes.
HELP_NAME  = os.getenv("SMH_HELP_NAME",  "Syuhada Binti Sooid")
HELP_EMAIL = os.getenv("SMH_HELP_EMAIL", "Syuhada_Sooid@jabil.com")


def smh_link(workcell: str) -> str:
    """Deep link to the SMH page, pre-filtered to this workcell's gaps."""
    return (f"{BASE_URL}/ietools/ole/smh"
            f"?workcell={quote(workcell)}&status=NOT_IN_SMH_DB")


def coverage() -> list[dict]:
    """Per-workcell coverage: total models, how many lack SMH, and the percent.

    The mart is a snapshot from the last pipeline run, so it is overlaid with
    the live `smh` table before counting — exactly what the SMH page does. Skip
    the overlay and you email someone about a model they filled in this morning,
    which is the fastest way to get the whole report ignored.
    """
    df = pd.read_parquet(MART["smh_status"])
    live = {(r["workcell"], r["assembly"])
            for r in smh_store.list_smh(limit=200_000) if r["smh_value"] > 0}

    df["has_smh"] = [(w, a) in live for w, a in zip(df["workcell"], df["assembly"])]

    out = []
    for wc, g in df.groupby("workcell"):
        missing = g[~g["has_smh"]]
        total = len(g)
        out.append({
            "workcell": wc,
            "total_models": total,
            "missing_models": len(missing),
            "missing_pct": round(len(missing) / total * 100, 1) if total else 0.0,
            "qty_unearned": int(missing["total_qty_produced"].sum()),
            "link": smh_link(wc),
        })
    return sorted(out, key=lambda r: r["missing_pct"], reverse=True)


def _recipients() -> dict[str, dict]:
    """Owners per workcell, from the access DB. Keyed by workcell.

    Imported here rather than at module load: api.routers pulls in FastAPI, and
    the CLI path has no reason to.
    """
    from api.routers.access import recipients
    return {w["workcell"]: w for w in recipients(key=NOTIFY_KEY)["workcells"]}


def build() -> list[dict]:
    """Every workcell, with its PIC attached. Worst coverage first.

    Workcells with a full house are kept, not filtered out — a zero next to a
    name is the only way the report can ever show someone finishing.
    """
    owners = _recipients()
    rows = []
    for row in coverage():
        who = owners.get(row["workcell"], {})
        names = [p["name"] for p in who.get("to", [])] + [p["name"] for p in who.get("cc", [])]
        rows.append({**row, "pic": ", ".join(names) if names else "—"})
    return rows


def render(rows: list[dict]) -> str:
    """The digest. Inline styles — mail clients drop <style> blocks.

    One table, one row per workcell, the workcell name being the link to its own
    filtered SMH page. Model numbers are deliberately NOT listed: the biggest
    workcell has 2,000 of them, and a list pasted into an email is stale the
    moment someone fixes one.
    """
    td  = "padding:9px 12px;border-bottom:1px solid #e5e7eb;font-size:14px"
    tdr = td + ";text-align:right;font-family:Consolas,monospace"
    th  = ("padding:8px 12px;background:#f3f4f6;text-align:left;font-size:11px;"
           "color:#6b7280;text-transform:uppercase;letter-spacing:.04em")
    thr = th + ";text-align:right"

    body = []
    for r in rows:
        gap = r["missing_models"] > 0
        colour = "#b91c1c" if r["missing_pct"] >= 25 else "#a16207" if gap else "#059669"
        body.append(f"""\
    <tr>
      <td style="{td}"><a href="{r['link']}" style="color:#0369a1;font-weight:600;
          text-decoration:none">{r['workcell']}</a></td>
      <td style="{tdr}">{r['total_models']:,}</td>
      <td style="{tdr};color:{colour};font-weight:700">{r['missing_models']:,}</td>
      <td style="{tdr};color:{colour};font-weight:700">{r['missing_pct']}%</td>
      <td style="{td};color:#374151">{r['pic']}</td>
    </tr>""")

    tot_models  = sum(r["total_models"] for r in rows)
    tot_missing = sum(r["missing_models"] for r in rows)
    tot_pct = round(tot_missing / tot_models * 100, 1) if tot_models else 0.0
    tot_qty = sum(r["qty_unearned"] for r in rows)

    return f"""\
<div style="font-family:Segoe UI,Arial,sans-serif;color:#111827;max-width:720px">
  <h2 style="margin:0 0 4px;font-size:19px">SMH Coverage</h2>
  <p style="margin:0 0 16px;color:#6b7280;font-size:13px">
    Models built in MES with no Standard Man Hours.</p>

  <p style="font-size:14px">
    <b>{tot_missing:,}</b> of <b>{tot_models:,}</b> models ({tot_pct}%) have no SMH.
    They earned <b>zero</b> standard hours across <b>{tot_qty:,}</b> units built,
    while the labour to build them still counts against OLE — so the reported
    OLE % is lower than the real one.</p>

  <table style="border-collapse:collapse;width:100%;margin:18px 0">
    <tr>
      <th style="{th}">Workcell</th>
      <th style="{thr}">Total models</th>
      <th style="{thr}">Missing SMH</th>
      <th style="{thr}">Missing %</th>
      <th style="{th}">PIC</th>
    </tr>
{chr(10).join(body)}
  </table>

  <p style="font-size:12px;color:#6b7280;line-height:1.6">
    <b>Click any workcell name</b> to open the SMH page already filtered to that
    workcell's missing models. If you have edit access you can fill them in
    there. A value applies at the next pipeline refresh — and then to past weeks
    too, since OLE is recomputed from the full history.</p>

  <div style="margin-top:22px;padding-top:14px;border-top:1px solid #e5e7eb;
              font-size:11.5px;color:#9ca3af;line-height:1.7">
    <b style="color:#6b7280">This is an automated email from IE Pulse.</b>
    Please do not reply — {SMTP_FROM} is not monitored.<br>
    For assistance, questions about these numbers, or to be added to or removed
    from this list, please contact
    <a href="mailto:{HELP_EMAIL}" style="color:#0369a1;font-weight:600;
       text-decoration:none">{HELP_NAME}</a>.
  </div>
</div>"""


def _connect() -> smtplib.SMTP:
    """First relay that answers. Raises if none do, naming everything tried."""
    errors = []
    for host in SMTP_HOSTS:
        try:
            smtp = smtplib.SMTP(host, SMTP_PORT, timeout=30)
            log.info("relay: %s:%d", host, SMTP_PORT)
            return smtp
        except OSError as e:
            errors.append(f"{host}: {e}")
    raise RuntimeError("No mail relay answered on port "
                       f"{SMTP_PORT}. Tried -- " + " | ".join(errors))


def send(rows: list[dict], to: list[str]) -> int:
    """One digest to everyone in `to`. Returns the recipient count."""
    if not BASE_URL:
        raise RuntimeError("PULSE_BASE_URL is not set -- every link would be broken.")
    if not to:
        raise RuntimeError("No recipients.")

    missing = sum(r["missing_models"] for r in rows)
    total = sum(r["total_models"] for r in rows)

    m = EmailMessage()
    m["Subject"] = f"[IE Pulse] SMH coverage - {missing:,} of {total:,} models missing SMH"
    m["From"] = SMTP_FROM
    m["To"] = ", ".join(to)
    # Plain-text alternative, not decoration: some clients and most mail
    # archivers only keep this part.
    m.set_content(
        f"{missing:,} of {total:,} models have no SMH.\n\n"
        + "\n".join(f"  {r['workcell']:<22} {r['missing_models']:>5} / "
                    f"{r['total_models']:<6} {r['missing_pct']:>5}%   {r['pic']}\n"
                    f"      {r['link']}" for r in rows)
        + f"\n\n--\nThis is an automated email from IE Pulse. Please do not reply"
          f" -- {SMTP_FROM} is not monitored.\nFor assistance, or to be added to"
          f" or removed from this list, contact {HELP_NAME} ({HELP_EMAIL}).\n"
    )
    m.add_alternative(render(rows), subtype="html")

    with _connect() as smtp:
        smtp.send_message(m)
    log.info("digest sent -> %s", to)
    return len(to)


def default_recipients() -> list[str]:
    """Everyone opted into `ole_smh`, deduped. Used when --to is not given."""
    seen = {}
    for w in _recipients().values():
        for p in w["to"] + w["cc"]:
            seen[p["email"].lower()] = p["email"]
    return sorted(seen.values())


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    ap = argparse.ArgumentParser(description="Email the SMH coverage digest.")
    ap.add_argument("--send", action="store_true", help="actually send (default: dry run)")
    ap.add_argument("--to", help="comma-separated recipients; default = everyone opted in")
    ap.add_argument("--html", help="write the rendered email to this path and stop")
    args = ap.parse_args()

    rows = build()
    if args.html:
        from pathlib import Path
        Path(args.html).write_text(render(rows), encoding="utf-8")
        print(f"Wrote {args.html}")
        return 0

    to = ([t.strip() for t in args.to.split(",") if t.strip()]
          if args.to else default_recipients())

    for r in rows:
        print(f"{r['workcell']:<22} {r['missing_models']:>5} / {r['total_models']:<6} "
              f"{r['missing_pct']:>5}%   {r['pic']}")
    print(f"\nTo: {', '.join(to)}")

    if not args.send:
        print("\nDRY RUN -- nothing sent. Re-run with --send.")
        return 0

    print(f"\nSent to {send(rows, to)} recipients.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
