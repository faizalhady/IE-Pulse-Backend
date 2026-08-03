# Auto-Ingest Setup — Windows Task Scheduler (every 6 hours)

This sets up automatic incremental refresh that preserves historical data
even when source CSVs get rotated out of the network share.

---

## How it works

| Mode | Command | What it does | When |
|---|---|---|---|
| **Incremental** (default) | `python pipeline/refresh.py` | Reads only NEW files since the last run's high-water mark. **Appends** to existing parquet marts. Preserves historical rows even if source CSVs have been deleted. | Routine / scheduled |
| **Full** | `python pipeline/refresh.py --full` | Wipes marts and re-reads everything currently visible in the share. **Anything no longer in the share is LOST.** | Disaster recovery only |

State is tracked in `data/mart/.ingest_state.json`:

```json
{
  "production":      "2026-05-11",
  "paid_hours_raw":  "2026-05-11",
  "last_run":        "2026-05-13T01:42:09",
  "last_run_mode":   "incremental"
}
```

On each incremental run we ask the share for "files with filename-date > high-water mark", merge them with the existing parquet, all-column dedup, and bump the high-water mark to the latest date we now know about.

---

## Step-by-step: schedule it every 6 hours

### 1. Find your `python.exe` path

In a terminal:

```bash
python -c "import sys; print(sys.executable)"
```

Copy the output — e.g. `C:\Python311\python.exe`.

### 2. Open Task Scheduler

`Win+R` → type `taskschd.msc` → Enter.

### 3. Create the task

**Right pane → "Create Task..."** (not "Create Basic Task" — we need the full editor).

**General tab:**
- **Name:** `OLE Incremental Refresh`
- **Description:** `Pulls latest production + paid-hours CSVs, appends to OLE marts.`
- **Security options:**
  - ✅ "Run whether user is logged on or not"
  - ✅ "Run with highest privileges" *(only if your account needs it for the network share)*
- **Configure for:** Windows 10 (or 11)

**Triggers tab → New...**
- Begin the task: **On a schedule**
- Settings: **Daily**
- Start: today at `00:00:00`
- Recur every `1` day
- ✅ **Repeat task every:** `6 hours` (type it; not in dropdown)
  - For a duration of: **1 day**
- ✅ Enabled
- OK

**Actions tab → New...**
- Action: **Start a program**
- Program/script: `<your python.exe path>` — e.g. `C:\Python311\python.exe`
- Add arguments: `pipeline/refresh.py`
- Start in: `C:\Users\4033375\Projects\IE-Pulse-Backend`
- OK

**Conditions tab:**
- ❌ Uncheck "Start the task only if the computer is on AC power" *(if it's a laptop)*
- ✅ "Wake the computer to run this task" *(if you want it to wake)*

**Settings tab:**
- ✅ "Allow task to be run on demand"
- ✅ "If the task fails, restart every:" `15 minutes`, attempt up to `3` times
- ✅ "Stop the task if it runs longer than: `2 hours`" *(refresh takes ~10 min; if it's running longer, something is wrong)*
- "If the task is already running, then the following rule applies: **Do not start a new instance**"

OK → enter password if prompted.

---

## 4. Test it once manually

In Task Scheduler, **right-click the task → Run**.

Watch:
- `data/mart/.ingest_state.json` should update with a fresh `last_run` timestamp
- `data/mart/raw_*.parquet` row counts should grow (incrementally) or stay the same (if no new files)
- Task History tab should show `(0x0)` = success

---

## When to run `--full` instead

Only run a full refresh when:
- You want to **rebuild marts from scratch** with whatever's currently in the share
- You've **changed a transform** that affects historical rows (e.g., normalization fix that should re-apply to past data)
- You've **wiped or corrupted** the marts
- You've **added a new workcell** to `WORKCELL_CONFIG` and want it backfilled for all dates *currently in the share*

Run manually:

```bash
cd "C:/Users/4033375/Projects/IE-Pulse-Backend"
python pipeline/refresh.py --full
```

Remember: anything no longer in the share will be **lost** from the new marts.

---

## Sanity checks

After a few scheduled runs, peek at the state file:

```bash
cat "C:/Users/4033375/Projects/IE-Pulse-Backend/data/mart/.ingest_state.json"
```

You should see `last_run` advancing every 6h and `production` / `paid_hours_raw` dates advancing whenever new CSVs arrive on the share.

If marts shrink unexpectedly, something is wrong — check Task Scheduler history for errors.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Task runs but mart row count is 0 | VPN dropped → share unreachable | Reconnect VPN, run manually |
| `last_run` not advancing | Task not actually firing | Check Triggers tab; check Task History |
| Mart shrinks after a run | Someone ran `--full` and source share was incomplete | Restore last good parquet from backup, never use `--full` while disconnected |
| New files visible in share but not in mart | High-water mark is wrong | Inspect `.ingest_state.json`, manually edit or delete to force re-scan |
