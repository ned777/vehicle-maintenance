#!/usr/bin/env python3
"""
Vehicle Maintenance Record — a small, dependency-free (stdlib only) HTTP
server. Stores everything in a SQLite file right next to this script, in
the "Vehicle Maintenance Record" folder. No pip installs, no venv, same
philosophy as the sysmon-widget agent: one file, plain Python, nothing to
break on an update.

Run:  python3 vmr_server.py
Serves on 0.0.0.0:8091, protected by HTTP Basic Auth (same mechanism the
Android app expects — see the Android project's Config.kt).
"""
import base64
import calendar
import datetime
import html
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8091
# Real credentials live in local_secrets.py, gitignored — see
# local_secrets.py.example. Falls back to an obviously-placeholder password
# so a fresh checkout without that file still runs, just isn't the real
# server (and makes it obvious the file needs to be created).
try:
    from local_secrets import AUTH_USER, AUTH_PASS
except ImportError:
    AUTH_USER = "admin"
    AUTH_PASS = "changeme"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maintenance.db")
ENTRIES_PER_PAGE = 100

# Each rule: how many miles and/or how many months before that service is
# due again, starting from the last logged entry of that type. A car's real
# owner's manual is "whichever comes first" — so is this.
SERVICE_RULES = [
    ("oil_change", "Oil Change", 5000, 6),
    ("tire_rotation", "Tire Rotation", 5000, 6),
    ("air_filter", "Engine Air Filter", 15000, 12),
    ("cabin_air_filter", "Cabin Air Filter", 15000, 12),
    ("brake_inspection", "Brake Inspection", 12000, 12),
    ("brake_fluid", "Brake Fluid Flush", 30000, 24),
    ("coolant_flush", "Coolant Flush", 30000, 30),
    ("transmission_fluid", "Transmission Fluid", 30000, 24),
    ("differential_oil", "Differential Oil", 30000, 24),
    ("spark_plugs", "Spark Plugs", 60000, None),
    ("battery_check", "Battery Check", None, 12),
    ("wiper_blades", "Wiper Blades", None, 12),
]
SERVICE_LABELS = {key: label for key, label, _, _ in SERVICE_RULES}


def parse_service_types(raw):
    """entries.service_type holds one or more keys as 'oil_change,tire_rotation'
    — one visit, one row, however many services it actually covered."""
    return [t for t in (raw or "").split(",") if t]


def service_types_label(raw):
    return ", ".join(SERVICE_LABELS.get(t, t) for t in parse_service_types(raw))


_local = threading.local()


def db():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA foreign_keys = ON")
    return _local.conn


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            current_mileage INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            entry_date TEXT NOT NULL,
            mileage INTEGER NOT NULL,
            service_type TEXT NOT NULL,
            cost TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            deleted_at TEXT
        );
        """
    )
    # Migration for a DB created before deleted_at existed (soft-delete/trash).
    try:
        conn.execute("ALTER TABLE entries ADD COLUMN deleted_at TEXT")
    except sqlite3.OperationalError:
        pass  # column already there
    conn.commit()
    conn.close()


# --- Date entry: three <select> lists instead of free text -----------------
# A plain text field only rejects garbage (letters, 14-digit numbers) at
# submit time via the HTML pattern attribute — and that client-side check
# isn't reliable enough to depend on. Three dropdowns make invalid input
# structurally impossible: there's nothing to type, only valid values to
# pick from.

def date_select_fields(selected_iso, name_prefix="entry"):
    year, month, day = (int(p) for p in selected_iso.split("-"))
    current_year = datetime.date.today().year
    year_options = "".join(
        f"<option value='{y}'{' selected' if y == year else ''}>{y}</option>"
        for y in range(current_year - 15, current_year + 2)
    )
    month_options = "".join(
        f"<option value='{m:02d}'{' selected' if m == month else ''}>{m:02d}</option>"
        for m in range(1, 13)
    )
    day_options = "".join(
        f"<option value='{d:02d}'{' selected' if d == day else ''}>{d:02d}</option>"
        for d in range(1, 32)
    )
    return (
        f"<div class='date-select-row'>"
        f"<select name='{name_prefix}_year' aria-label='Year'>{year_options}</select>"
        f"<select name='{name_prefix}_month' aria-label='Month'>{month_options}</select>"
        f"<select name='{name_prefix}_day' aria-label='Day'>{day_options}</select>"
        f"</div>"
    )


def combine_date(form, name_prefix="entry"):
    """Reads the three dropdowns back and clamps to the real last day of that
    month (so e.g. picking Feb 30 lands on Feb 28/29 instead of producing a
    date string later code can't parse)."""
    try:
        year = int(form.get(f"{name_prefix}_year", "0"))
        month = int(form.get(f"{name_prefix}_month", "1"))
        day = int(form.get(f"{name_prefix}_day", "1"))
        day = min(day, calendar.monthrange(year, month)[1])
        return datetime.date(year, month, day).isoformat()
    except (ValueError, TypeError):
        return datetime.date.today().isoformat()


# --- Reminder engine ---------------------------------------------------

def _add_months(d, months):
    total = d.year * 12 + (d.month - 1) + months
    return datetime.date(total // 12, total % 12 + 1, 1)


def compute_reminders(vehicle):
    """One row per known service type: last time it was done (if ever),
    and a status of ok / due_soon / overdue / unknown."""
    conn = db()
    today = datetime.date.today()

    # One entry can cover several service types at once (see
    # parse_service_types), so this can't be a simple per-type SQL WHERE —
    # pull every entry once, newest first, and pick the first row that
    # mentions each type.
    all_entries = conn.execute(
        "SELECT entry_date, mileage, service_type FROM entries WHERE vehicle_id=? AND deleted_at IS NULL "
        "ORDER BY entry_date DESC, id DESC",
        (vehicle["id"],),
    ).fetchall()

    rows = []
    for key, label, miles_interval, months_interval in SERVICE_RULES:
        last = next((e for e in all_entries if key in parse_service_types(e["service_type"])), None)

        if last is None:
            rows.append({"key": key, "label": label, "status": "unknown", "detail": "No record yet"})
            continue

        last_date = datetime.date.fromisoformat(last["entry_date"])
        last_mileage = last["mileage"]

        miles_used_frac = None
        if miles_interval:
            miles_since = vehicle["current_mileage"] - last_mileage
            miles_used_frac = miles_since / miles_interval

        months_used_frac = None
        if months_interval:
            months_since = (today.year - last_date.year) * 12 + (today.month - last_date.month)
            months_used_frac = months_since / months_interval

        used_frac = max([f for f in (miles_used_frac, months_used_frac) if f is not None], default=0)

        if used_frac >= 1.15:
            status = "overdue"
        elif used_frac >= 0.9:
            status = "due_soon"
        else:
            status = "ok"

        detail_bits = []
        if miles_interval:
            remaining = miles_interval - (vehicle["current_mileage"] - last_mileage)
            detail_bits.append(f"{remaining:,} mi left" if remaining >= 0 else f"{-remaining:,} mi over")
        if months_interval:
            due_date = _add_months(last_date, months_interval)
            if due_date < today:
                detail_bits.append(f"overdue since {due_date.isoformat()}")
            else:
                detail_bits.append(f"due {due_date.isoformat()}")
        rows.append({
            "key": key, "label": label, "status": status,
            "detail": " · ".join(detail_bits) if detail_bits else "",
            "last_date": last_date.isoformat(), "last_mileage": last_mileage,
        })

    order = {"overdue": 0, "due_soon": 1, "unknown": 2, "ok": 3}
    rows.sort(key=lambda r: order[r["status"]])
    return rows


# --- HTML shell ----------------------------------------------------------

CSS = """
:root {
  --bg: #000000;
  --surface: #0D0D0F;
  --surface-2: #17171A;
  --border: #2B2B30;
  --text: #ECECEF;
  --text-dim: #8B8B93;
  --accent: #D21F2E;
  --accent-hover: #E8404D;
  --ok: #2FB35C;
  --warn: #E0A526;
  --danger: #E8404D;
  --chrome: #4C4C53;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: 'Barlow', 'Segoe UI', system-ui, sans-serif;
  font-size: 16px; line-height: 1.55;
}
h1, h2, h3, .num {
  font-family: 'Rajdhani', 'Barlow', sans-serif;
  font-weight: 700;
  letter-spacing: 0.01em;
}
.num { font-variant-numeric: tabular-nums; }
a { color: var(--text); text-decoration: none; }
.wrap { max-width: 880px; margin: 0 auto; padding: 0 1.25rem 4rem; }
header.top {
  border-bottom: 1px solid var(--border);
  padding: 1.5rem 0 1.1rem;
  display: flex; align-items: baseline; justify-content: space-between; gap: 1rem;
}
header.top .brand { display: flex; align-items: center; gap: 0.6rem; }
header.top .brand .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
header.top h1 { font-size: 1.5rem; margin: 0; text-transform: uppercase; }
header.top a.back { font-size: 0.85rem; color: var(--text-dim); border-bottom: 1px dotted var(--chrome); }
header.top a.back:hover { color: var(--text); }

.vehicle-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 1.3rem 1.4rem; margin: 1.4rem 0;
}
.vehicle-card .row-head { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; flex-wrap: wrap; }
.vehicle-card h2 { margin: 0; font-size: 1.3rem; }
/* The card's heading IS the odometer reading now (the vehicle name already
   shows once, in the page header above) — "Odometer" reads as a dim label,
   the number itself carries the actual heading weight/size. */
.vehicle-card .mileage { margin: 0; font-size: 1.3rem; }
.vehicle-card .mileage-label { color: var(--text-dim); font-size: 0.7em; text-transform: uppercase; letter-spacing: 0.05em; margin-right: 0.35em; }
.vehicle-card .mileage .num { font-size: 1.15em; }

.pill-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1rem; }
.pill {
  display: flex; align-items: center; gap: 0.45rem;
  border: 1px solid var(--border); border-radius: 999px;
  padding: 0.35rem 0.75rem 0.35rem 0.6rem; font-size: 0.82rem; background: var(--surface-2);
}
.pill .led { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.pill.overdue .led { background: var(--danger); box-shadow: 0 0 6px var(--danger); }
.pill.due_soon .led { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
.pill.ok .led { background: var(--ok); }
.pill.unknown .led { background: var(--chrome); }
.pill .lbl { color: var(--text); font-weight: 600; }
.pill .det { color: var(--text-dim); }

.actions { margin-top: 1.1rem; display: flex; gap: 0.7rem; flex-wrap: wrap; }
.btn {
  display: inline-block; padding: 0.55rem 1rem; border-radius: 6px;
  font-weight: 600; font-size: 0.88rem; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text);
}
.btn:hover { border-color: var(--chrome); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }

/* The scroll (if the table ever still doesn't fit) stays contained to the
   table itself, not the whole page — so the header/cards above it never
   shift out of alignment with the viewport edge. */
.table-wrap { overflow-x: auto; margin-top: 1rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
/* vertical-align: top matters here — a wrapped two-line Service label makes
   that row taller, and without this every other short single-line cell in
   the same row (Mileage, Cost, Actions) would center itself in that extra
   height instead of sitting flush at the top, which is what actually made
   the divider lines look unevenly spaced row to row. */
th, td { text-align: left; vertical-align: top; padding: 0.6rem 0.5rem; border-bottom: 1px solid var(--border); }
th { color: var(--text-dim); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:hover { background: var(--surface-2); }
/* This flex row lives on a plain <div> INSIDE the <td>, not on the <td>
   itself — setting display:flex directly on a <td> makes some browsers spin
   up an anonymous table-cell wrapper around it for table layout purposes,
   and that wrapper (not the real <td>) is what actually ends up governed by
   vertical-align, silently ignoring the `vertical-align: top` rule above.
   Keeping the <td> a plain, unstyled table-cell sidesteps that entirely. */
.row-actions { display: flex; align-items: center; gap: 0.4rem; white-space: nowrap; }
/* display:contents makes the <form> wrapping Delete/Restore's <button>
   generate no box of its own — its child button becomes a direct flex item
   of .row-actions, same as Edit's plain <a>. Without this, the FORM (a
   block-level wrapper) is what .row-actions actually sees as the flex item,
   and the container's default align-items:stretch then stretches Edit's
   bare <a> to match the form's own height rather than the button's real
   size — which is what made Edit and Delete come out different heights. */
.row-actions form { display: contents; }

/* Edit is an <a>, Delete/Restore are <button>s inside a <form> — browsers
   apply their own UA defaults to <button> (a baked-in min-height, its own
   line-height/appearance) that an <a> never gets, which is what made Edit
   and Delete come out different sizes even with identical CSS. appearance:
   none plus explicit line-height/border/margin strips all of that so every
   variant, anchor or button, resolves to the exact same box. A fixed width
   (sized to fit "Delete forever," the longest label used anywhere) means
   none of them hug their own text either — same width, same height, always. */
.action-pill {
  -webkit-appearance: none; appearance: none;
  display: inline-flex; align-items: center; justify-content: center;
  width: 108px; margin: 0; line-height: 1.15;
  font-family: 'Rajdhani', 'Barlow', sans-serif;
  font-size: 0.7rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
  padding: 0.42rem 0.5rem; border-radius: 6px; cursor: pointer;
  background: var(--surface-2); transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.action-pill.edit { border: 1px solid var(--chrome); color: var(--text); }
.action-pill.edit:hover { border-color: var(--accent); color: var(--accent); }
.action-pill.delete { border: 1px solid color-mix(in srgb, var(--danger) 55%, var(--border)); color: var(--danger); }
.action-pill.delete:hover { background: var(--danger); border-color: var(--danger); color: #fff; }
.action-pill.restore { border: 1px solid color-mix(in srgb, var(--ok) 55%, var(--border)); color: var(--ok); }
.action-pill.restore:hover { background: var(--ok); border-color: var(--ok); color: #06210f; }

/* A phone (and the Android app's WebView, same narrow width) can't fit six
   columns of Date/Service/Mileage/Cost/Notes/Actions side by side no matter
   how small the text gets — so below this width the table stops being a
   table at all and becomes one stacked card per row instead, each field
   labeled by the data-label attribute the Python side already sets on
   every <td>. Zero horizontal scrolling either way, by construction. */
@media (max-width: 560px) {
  .table-wrap { overflow-x: visible; }
  table, thead, tbody, tr { display: block; width: 100%; }
  thead { display: none; }
  tr {
    border: 1px solid var(--border); border-radius: 10px;
    padding: 0.85rem 1rem; margin-bottom: 0.8rem; background: var(--surface);
  }
  td {
    display: flex; justify-content: space-between; align-items: baseline; gap: 1rem;
    border-bottom: none; padding: 0.3rem 0; text-align: right;
  }
  td.num { text-align: right; }
  td::before {
    content: attr(data-label);
    font-family: ui-monospace, 'SF Mono', Consolas, monospace; font-size: 0.68rem; letter-spacing: 0.04em;
    text-transform: uppercase; color: var(--text-dim); text-align: left; flex: none;
  }
  td[data-label='Notes'] { align-items: flex-start; }
  td[data-label='Notes']:empty::before { content: none; }
  td.actions-cell { display: block; padding-top: 0.6rem; }
  .row-actions { justify-content: flex-end; }
}

.pager { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-top: 1.2rem; }
.pager-status { color: var(--text-dim); font-size: 0.85rem; }
.trash-note { color: var(--text-dim); font-size: 0.85rem; margin: -0.4rem 0 1rem; }

/* Android's WebView (the Garage app) never implements window.confirm() —
   it just silently no-ops, so a tap on Delete there does nothing at all,
   with no dialog and no error. A real desktop/mobile browser DOES support
   confirm(), which is why this looked fine on web. This modal is plain
   HTML/CSS/JS with no dependency on that browser API, so it behaves
   identically in the app and in a browser. */
.confirm-overlay {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.7);
  display: flex; align-items: center; justify-content: center;
  padding: 1.5rem; z-index: 1000;
}
.confirm-overlay[hidden] { display: none; }
.confirm-box {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 1.4rem 1.5rem; max-width: 360px; width: 100%;
}
.confirm-box p { margin: 0 0 1.2rem; font-size: 0.98rem; line-height: 1.5; }
.confirm-actions { display: flex; justify-content: flex-end; gap: 0.6rem; }

form.stack { display: flex; flex-direction: column; gap: 0.9rem; max-width: 420px; margin-top: 1.2rem; }
form.stack label { font-size: 0.82rem; color: var(--text-dim); display: flex; flex-direction: column; gap: 0.35rem; }
input, select, textarea {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px;
  color: var(--text); padding: 0.55rem 0.65rem; font-size: 0.95rem; font-family: inherit;
}
input:focus, select:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: 1px; border-color: var(--accent); }
textarea { resize: vertical; min-height: 4.5em; }

.date-select-row { display: flex; gap: 0.5rem; margin-top: -0.55rem; }
.date-select-row select { flex: 1; min-width: 0; }

.check-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 0.5rem; max-width: 100%;
}
.check-grid label {
  flex-direction: row; align-items: center; gap: 0.55rem;
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 0.55rem 0.7rem; color: var(--text); font-size: 0.88rem; cursor: pointer;
}
.check-grid input[type=checkbox] {
  width: 17px; height: 17px; accent-color: var(--accent); padding: 0; flex: none;
}
.check-grid label:has(input:checked) { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, var(--surface-2)); }

.empty { color: var(--text-dim); padding: 1.5rem 0; }
.section-title { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-dim); margin: 1.6rem 0 0.5rem; }

/* A phone/WebView viewport never hits this — it's the narrow-screen Android
   app view people already like. This is purely for a desktop browser window,
   where the same fixed sizes above read as too small on a big monitor. */
@media (min-width: 820px) {
  body { font-size: 19px; }
  .wrap { max-width: 1180px; padding: 0 2rem 5rem; }
  header.top { padding: 2.2rem 0 1.5rem; }
  header.top h1 { font-size: 2rem; }
  header.top .brand .dot { width: 13px; height: 13px; }
  header.top a.back { font-size: 1rem; }

  .vehicle-card { padding: 1.9rem 2.1rem; margin: 1.9rem 0; border-radius: 14px; }
  .vehicle-card h2 { font-size: 1.75rem; }
  .vehicle-card .mileage { font-size: 1.75rem; }

  .pill { font-size: 0.98rem; padding: 0.5rem 1rem 0.5rem 0.75rem; gap: 0.55rem; }
  .pill .led { width: 10px; height: 10px; }

  .btn { font-size: 1.02rem; padding: 0.7rem 1.3rem; border-radius: 8px; }

  table { font-size: 1.05rem; }
  th, td { padding: 0.85rem 0.7rem; }
  th { font-size: 0.8rem; }

  form.stack { max-width: 560px; gap: 1.2rem; }
  form.stack label { font-size: 0.92rem; }
  input, select, textarea { font-size: 1.05rem; padding: 0.7rem 0.85rem; }
  .check-grid { grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 0.65rem; }
  .check-grid label { font-size: 1rem; padding: 0.65rem 0.85rem; }

  .section-title { font-size: 0.88rem; margin: 2rem 0 0.7rem; }
}
"""

HEAD = (
    "<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Vehicle Maintenance Record</title>"
    "<link rel='icon' href=\"data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    "<text y='.9em' font-size='90'>%F0%9F%94%A7</text></svg>\">"
    "<link rel='preconnect' href='https://fonts.googleapis.com'>"
    "<link href='https://fonts.googleapis.com/css2?family=Rajdhani:wght@600;700&family=Barlow:wght@400;500;600&display=swap' rel='stylesheet'>"
    f"<style>{CSS}</style>"
)


CONFIRM_MODAL = (
    "<div class='confirm-overlay' id='confirmOverlay' hidden>"
    "<div class='confirm-box'><p id='confirmMessage'></p>"
    "<div class='confirm-actions'>"
    "<button type='button' class='btn' id='confirmCancelBtn'>Cancel</button>"
    "<button type='button' class='btn primary' id='confirmOkBtn'>Confirm</button>"
    "</div></div></div>"
    "<script>"
    "(function(){"
    "var pending=null;"
    "var overlay=document.getElementById('confirmOverlay');"
    "var msg=document.getElementById('confirmMessage');"
    "window.confirmAction=function(form,message){"
    "pending=form;msg.textContent=message;overlay.hidden=false;return false;"
    "};"
    "document.getElementById('confirmCancelBtn').addEventListener('click',function(){"
    "pending=null;overlay.hidden=true;"
    "});"
    "document.getElementById('confirmOkBtn').addEventListener('click',function(){"
    "overlay.hidden=true;if(pending){pending.submit();}"
    "});"
    "})();"
    "</script>"
)


def page(title, body_html, back_href=None, back_label=None):
    back = f"<a class='back' href='{back_href}'>&larr; {html.escape(back_label or 'Back')}</a>" if back_href else "<span></span>"
    return (
        f"<!doctype html><html><head>{HEAD}</head><body><div class='wrap'>"
        f"<header class='top'><div class='brand'><span class='dot'></span><h1>{html.escape(title)}</h1></div>{back}</header>"
        f"{body_html}"
        f"</div>{CONFIRM_MODAL}</body></html>"
    )


def status_pill(r):
    return (
        f"<div class='pill {r['status']}'><span class='led'></span>"
        f"<span class='lbl'>{html.escape(r['label'])}</span>"
        f"<span class='det'>{html.escape(r['detail'])}</span></div>"
    )


def render_dashboard():
    conn = db()
    vehicles = conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
    if not vehicles:
        body = (
            "<p class='empty'>No vehicles yet.</p>"
            "<div class='actions'><a class='btn primary' href='/vehicles/new'>+ Add a vehicle</a></div>"
        )
        return page("Garage", body)

    cards = []
    for v in vehicles:
        reminders = compute_reminders(v)
        flagged = [r for r in reminders if r["status"] in ("overdue", "due_soon")]
        pills = "".join(status_pill(r) for r in flagged) if flagged else "<p class='det' style='color:var(--ok)'>Everything's current.</p>"
        cards.append(
            f"<div class='vehicle-card'>"
            f"<div class='row-head'>"
            f"<h2><a href='/vehicle/{v['id']}'>{html.escape(v['name'])}</a></h2>"
            f"<div class='mileage'>Odometer <span class='num'>{v['current_mileage']:,}</span> mi</div>"
            f"</div>"
            f"<div class='pill-row'>{pills}</div>"
            f"<div class='actions'>"
            f"<a class='btn' href='/vehicle/{v['id']}'>View log</a>"
            f"<a class='btn primary' href='/vehicle/{v['id']}/log/new'>+ Log service</a>"
            f"</div></div>"
        )
    body = "".join(cards) + "<div class='actions'><a class='btn' href='/vehicles/new'>+ Add another vehicle</a></div>"
    return page("Garage", body)


def render_new_vehicle_form():
    body = (
        "<form class='stack' method='post' action='/vehicles/new'>"
        "<label>Vehicle name<input name='name' placeholder='2019 Honda Civic' required></label>"
        "<label>Current mileage<input name='mileage' type='number' min='0' placeholder='42000' required></label>"
        "<div class='actions'><button class='btn primary' type='submit'>Add vehicle</button></div>"
        "</form>"
    )
    return page("Add Vehicle", body, back_href="/", back_label="Garage")


def render_vehicle(vehicle_id, page_num=1):
    conn = db()
    v = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if v is None:
        return None
    reminders = compute_reminders(v)  # always over the FULL history, never just the current page

    total_entries = conn.execute(
        "SELECT COUNT(*) AS n FROM entries WHERE vehicle_id=? AND deleted_at IS NULL", (vehicle_id,)
    ).fetchone()["n"]
    total_pages = max(1, -(-total_entries // ENTRIES_PER_PAGE))  # ceiling division
    page_num = min(page_num, total_pages)
    entries = conn.execute(
        "SELECT * FROM entries WHERE vehicle_id=? AND deleted_at IS NULL ORDER BY entry_date DESC, id DESC "
        "LIMIT ? OFFSET ?",
        (vehicle_id, ENTRIES_PER_PAGE, (page_num - 1) * ENTRIES_PER_PAGE),
    ).fetchall()
    trash_count = conn.execute(
        "SELECT COUNT(*) AS n FROM entries WHERE vehicle_id=? AND deleted_at IS NOT NULL", (vehicle_id,)
    ).fetchone()["n"]

    pills = "".join(status_pill(r) for r in reminders)

    if entries:
        rows = []
        for e in entries:
            label = service_types_label(e["service_type"]) or e["service_type"]
            cost = html.escape(e["cost"]) if e["cost"] else "—"
            notes = html.escape(e["notes"]) if e["notes"] else ""
            rows.append(
                f"<tr><td data-label='Date'>{e['entry_date']}</td><td data-label='Service'>{html.escape(label)}</td>"
                f"<td class='num' data-label='Mileage'>{e['mileage']:,}</td><td class='num' data-label='Cost'>{cost}</td>"
                f"<td data-label='Notes'>{notes}</td>"
                f"<td class='actions-cell'><div class='row-actions'>"
                f"<a class='action-pill edit' href='/entry/{e['id']}/edit'>Edit</a>"
                f"<form method='post' action='/entry/{e['id']}/delete'>"
                f"<button class='action-pill delete' type='submit' "
                f"onclick='return confirmAction(this.form, \"Move this entry to trash? You can restore it later from the trash.\")'>Delete</button>"
                f"</form></div></td></tr>"
            )
        table = (
            "<div class='table-wrap'><table><thead><tr><th>Date</th><th>Service</th><th class='num'>Mileage</th>"
            "<th class='num'>Cost</th><th>Notes</th><th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        table = "<p class='empty'>No entries logged yet.</p>"

    if total_pages > 1:
        prev_link = f"<a class='btn' href='/vehicle/{v['id']}?page={page_num - 1}'>&larr; Newer</a>" if page_num > 1 else "<span></span>"
        next_link = f"<a class='btn' href='/vehicle/{v['id']}?page={page_num + 1}'>Older &rarr;</a>" if page_num < total_pages else "<span></span>"
        table += (
            f"<div class='pager'>{prev_link}"
            f"<span class='pager-status'>Page {page_num} of {total_pages}</span>"
            f"{next_link}</div>"
        )

    trash_btn = f"<a class='btn' href='/vehicle/{v['id']}/trash'>Trash ({trash_count})</a>" if trash_count else ""

    body = (
        f"<div class='vehicle-card'>"
        f"<div class='row-head'><h2 class='mileage'><span class='mileage-label'>Odometer</span>"
        f"<span class='num'>{v['current_mileage']:,}</span> mi</h2></div>"
        f"<div class='pill-row'>{pills}</div>"
        f"<div class='actions'>"
        f"<a class='btn primary' href='/vehicle/{v['id']}/log/new'>+ Log service</a>"
        f"<a class='btn' href='/vehicle/{v['id']}/mileage'>Update mileage</a>"
        f"{trash_btn}"
        f"</div></div>"
        f"<div class='section-title'>Service history</div>"
        f"{table}"
    )
    return page(v["name"], body, back_href="/", back_label="Garage")


def render_trash(vehicle_id):
    conn = db()
    v = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if v is None:
        return None
    entries = conn.execute(
        "SELECT * FROM entries WHERE vehicle_id=? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
        (vehicle_id,),
    ).fetchall()

    if entries:
        rows = []
        for e in entries:
            label = service_types_label(e["service_type"]) or e["service_type"]
            cost = html.escape(e["cost"]) if e["cost"] else "—"
            notes = html.escape(e["notes"]) if e["notes"] else ""
            rows.append(
                f"<tr><td data-label='Date'>{e['entry_date']}</td><td data-label='Service'>{html.escape(label)}</td>"
                f"<td class='num' data-label='Mileage'>{e['mileage']:,}</td><td class='num' data-label='Cost'>{cost}</td>"
                f"<td data-label='Notes'>{notes}</td>"
                f"<td class='actions-cell'><div class='row-actions'>"
                f"<form method='post' action='/entry/{e['id']}/restore'>"
                f"<button class='action-pill restore' type='submit'>Restore</button></form>"
                f"<form method='post' action='/entry/{e['id']}/delete-forever'>"
                f"<button class='action-pill delete' type='submit' "
                f"onclick='return confirmAction(this.form, \"Permanently delete this entry? This cannot be undone.\")'>Delete forever</button>"
                f"</form></div></td></tr>"
            )
        table = (
            "<div class='table-wrap'><table><thead><tr><th>Date</th><th>Service</th><th class='num'>Mileage</th>"
            "<th class='num'>Cost</th><th>Notes</th><th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
        empty_btn = (
            f"<div class='actions'><form method='post' action='/vehicle/{v['id']}/trash/empty'>"
            f"<button class='btn' type='submit' onclick='return confirmAction(this.form, "
            f"\"Permanently delete all {len(entries)} item(s) in trash? This cannot be undone.\")'>"
            f"Empty Trash</button></form></div>"
        )
    else:
        table = "<p class='empty'>Trash is empty.</p>"
        empty_btn = ""

    body = (
        f"<p class='trash-note'>Deleted entries land here first and don't count toward reminders. "
        f"Restore to bring one back, or delete forever to remove it for good.</p>"
        f"{empty_btn}"
        f"{table}"
    )
    return page(f"Trash — {v['name']}", body, back_href=f"/vehicle/{v['id']}", back_label=v["name"])


def render_new_entry_form(vehicle_id):
    conn = db()
    v = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if v is None:
        return None
    checkboxes = "".join(
        f"<label><input type='checkbox' name='service_type' value='{key}'>{html.escape(label)}</label>"
        for key, label, _, _ in SERVICE_RULES
    )
    checkboxes += "<label><input type='checkbox' name='service_type' value='other'>Other</label>"
    today = datetime.date.today().isoformat()
    body = (
        f"<form class='stack' method='post' action='/vehicle/{vehicle_id}/log/new'>"
        f"<label>Date</label>{date_select_fields(today)}"
        f"<label>Service type(s) &mdash; check all that applied this visit</label>"
        f"<div class='check-grid'>{checkboxes}</div>"
        f"<label>Mileage at service<input name='mileage' type='number' min='0' value='{v['current_mileage']}' required></label>"
        f"<label>Cost (optional)<input name='cost' placeholder='45.00'></label>"
        f"<label>Notes (optional)<textarea name='notes' placeholder='Synthetic oil, rotated tires too'></textarea></label>"
        f"<div class='actions'><button class='btn primary' type='submit'>Save entry</button></div>"
        f"</form>"
    )
    return page(f"Log service — {v['name']}", body, back_href=f"/vehicle/{vehicle_id}", back_label=v["name"])


def render_edit_entry_form(entry_id):
    conn = db()
    e = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if e is None:
        return None
    v = conn.execute("SELECT * FROM vehicles WHERE id=?", (e["vehicle_id"],)).fetchone()
    current_types = parse_service_types(e["service_type"])
    checkboxes = "".join(
        f"<label><input type='checkbox' name='service_type' value='{key}'"
        f"{' checked' if key in current_types else ''}>{html.escape(label)}</label>"
        for key, label, _, _ in SERVICE_RULES
    )
    checkboxes += (
        f"<label><input type='checkbox' name='service_type' value='other'"
        f"{' checked' if 'other' in current_types else ''}>Other</label>"
    )
    cost = html.escape(e["cost"] or "")
    notes = html.escape(e["notes"] or "")
    body = (
        f"<form class='stack' method='post' action='/entry/{entry_id}/edit' "
        f"onsubmit='return confirmAction(this, \"Save these changes?\")'>"
        f"<label>Date</label>{date_select_fields(e['entry_date'])}"
        f"<label>Service type(s) &mdash; check all that applied this visit</label>"
        f"<div class='check-grid'>{checkboxes}</div>"
        f"<label>Mileage at service<input name='mileage' type='number' min='0' value='{e['mileage']}' required></label>"
        f"<label>Cost (optional)<input name='cost' value='{cost}' placeholder='45.00'></label>"
        f"<label>Notes (optional)<textarea name='notes' placeholder='Synthetic oil, rotated tires too'>{notes}</textarea></label>"
        f"<div class='actions'>"
        f"<button class='btn primary' type='submit'>Save changes</button>"
        f"<a class='btn' href='/vehicle/{v['id']}'>Cancel</a>"
        f"</div>"
        f"</form>"
    )
    return page(f"Edit entry — {v['name']}", body, back_href=f"/vehicle/{v['id']}", back_label=v["name"])


def render_mileage_form(vehicle_id):
    conn = db()
    v = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if v is None:
        return None
    body = (
        f"<form class='stack' method='post' action='/vehicle/{vehicle_id}/mileage'>"
        f"<label>Current mileage<input name='mileage' type='number' min='0' value='{v['current_mileage']}' required></label>"
        f"<div class='actions'><button class='btn primary' type='submit'>Update</button></div>"
        f"</form>"
    )
    return page(f"Update mileage — {v['name']}", body, back_href=f"/vehicle/{vehicle_id}", back_label=v["name"])


# --- HTTP handler ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "VMR/1.0"

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; nothing sensitive, just less noise

    def _check_auth(self):
        header = self.headers.get("Authorization")
        expected = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
        if header != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Vehicle Maintenance Record"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _send_html(self, html_body, status=200):
        encoded = html_body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        # Without this, hitting the back button (e.g. leaving the trash page
        # after emptying it) can restore the browser's cached snapshot of the
        # PREVIOUS page instead of re-fetching it — the vehicle page then
        # shows a stale "Trash (3)" count until a manual reload. no-store
        # rules out both that disk/memory cache and the back/forward cache.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_found(self):
        self._send_html(page("Not Found", "<p class='empty'>Nothing here.</p>", back_href="/", back_label="Garage"), status=404)

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse_qs(raw)
        # Stashed on self so a handler that needs every value for a repeated
        # checkbox name (see service_type below) can still get at it — parse_qs
        # already groups repeats into a list, _read_form just flattens for the
        # common single-value case.
        self._parsed_form = parsed
        return {k: v[0] for k, v in parsed.items()}

    def _form_list(self, key):
        return self._parsed_form.get(key, [])

    def do_GET(self):
        if not self._check_auth():
            return
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        parts = [p for p in path.split("/") if p]
        query = parse_qs(parsed_url.query)

        if not parts:
            return self._send_html(render_dashboard())
        if parts == ["vehicles", "new"]:
            return self._send_html(render_new_vehicle_form())
        if len(parts) == 2 and parts[0] == "vehicle" and parts[1].isdigit():
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
            except ValueError:
                page = 1
            out = render_vehicle(int(parts[1]), page)
            return self._send_html(out) if out else self._not_found()
        if len(parts) == 3 and parts[0] == "vehicle" and parts[1].isdigit() and parts[2:] == ["mileage"]:
            out = render_mileage_form(int(parts[1]))
            return self._send_html(out) if out else self._not_found()
        if len(parts) == 3 and parts[0] == "vehicle" and parts[1].isdigit() and parts[2:] == ["trash"]:
            out = render_trash(int(parts[1]))
            return self._send_html(out) if out else self._not_found()
        if len(parts) == 4 and parts[0] == "vehicle" and parts[1].isdigit() and parts[2:] == ["log", "new"]:
            out = render_new_entry_form(int(parts[1]))
            return self._send_html(out) if out else self._not_found()
        if len(parts) == 3 and parts[0] == "entry" and parts[1].isdigit() and parts[2:] == ["edit"]:
            out = render_edit_entry_form(int(parts[1]))
            return self._send_html(out) if out else self._not_found()
        self._not_found()

    def do_POST(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        form = self._read_form()
        conn = db()

        if parts == ["vehicles", "new"]:
            name = form.get("name", "").strip()
            try:
                mileage = int(form.get("mileage", "0"))
            except ValueError:
                mileage = 0
            if name:
                conn.execute(
                    "INSERT INTO vehicles (name, current_mileage, created_at) VALUES (?, ?, ?)",
                    (name, mileage, datetime.datetime.now().isoformat()),
                )
                conn.commit()
            return self._redirect("/")

        if len(parts) == 4 and parts[0] == "vehicle" and parts[1].isdigit() and parts[2:] == ["log", "new"]:
            vehicle_id = int(parts[1])
            # One visit can cover several services at once (e.g. CVT fluid +
            # air filter, same day/mileage) — the form sends one or more
            # checked service_type values, all stored together on ONE row as
            # "oil_change,tire_rotation" (see parse_service_types) so it's
            # one row per visit, not one row per service.
            service_types = self._form_list("service_type") or ["other"]
            try:
                mileage = int(form.get("mileage", "0"))
            except ValueError:
                mileage = 0
            entry_date = combine_date(form)
            cost = form.get("cost", "").strip()
            notes = form.get("notes", "").strip()
            created_at = datetime.datetime.now().isoformat()
            conn.execute(
                "INSERT INTO entries (vehicle_id, entry_date, mileage, service_type, cost, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (vehicle_id, entry_date, mileage, ",".join(service_types), cost, notes, created_at),
            )
            # Logging a service at a mileage higher than what's on record bumps the odometer too.
            conn.execute(
                "UPDATE vehicles SET current_mileage = MAX(current_mileage, ?) WHERE id=?",
                (mileage, vehicle_id),
            )
            conn.commit()
            return self._redirect(f"/vehicle/{vehicle_id}")

        if len(parts) == 3 and parts[0] == "vehicle" and parts[1].isdigit() and parts[2:] == ["mileage"]:
            vehicle_id = int(parts[1])
            try:
                mileage = int(form.get("mileage", "0"))
            except ValueError:
                mileage = 0
            conn.execute("UPDATE vehicles SET current_mileage=? WHERE id=?", (mileage, vehicle_id))
            conn.commit()
            return self._redirect(f"/vehicle/{vehicle_id}")

        if len(parts) == 3 and parts[0] == "entry" and parts[1].isdigit() and parts[2:] == ["edit"]:
            entry_id = int(parts[1])
            existing = conn.execute("SELECT vehicle_id, service_type FROM entries WHERE id=?", (entry_id,)).fetchone()
            if existing is None:
                return self._not_found()
            vehicle_id = existing["vehicle_id"]
            # Same idea as a new log entry: one or more services can be
            # checked, all stored together on this one row.
            service_types = self._form_list("service_type") or parse_service_types(existing["service_type"])
            try:
                mileage = int(form.get("mileage", "0"))
            except ValueError:
                mileage = 0
            entry_date = combine_date(form)
            cost = form.get("cost", "").strip()
            notes = form.get("notes", "").strip()
            conn.execute(
                "UPDATE entries SET entry_date=?, mileage=?, service_type=?, cost=?, notes=? WHERE id=?",
                (entry_date, mileage, ",".join(service_types), cost, notes, entry_id),
            )
            conn.execute(
                "UPDATE vehicles SET current_mileage = MAX(current_mileage, ?) WHERE id=?",
                (mileage, vehicle_id),
            )
            conn.commit()
            return self._redirect(f"/vehicle/{vehicle_id}")

        if len(parts) == 3 and parts[0] == "entry" and parts[1].isdigit() and parts[2:] == ["delete"]:
            # Soft delete: stamp deleted_at instead of removing the row, so it
            # drops out of the log/reminders immediately but can be restored
            # from the trash rather than being gone for good.
            entry_id = int(parts[1])
            row = conn.execute("SELECT vehicle_id FROM entries WHERE id=?", (entry_id,)).fetchone()
            conn.execute("UPDATE entries SET deleted_at=? WHERE id=?", (datetime.datetime.now().isoformat(), entry_id))
            conn.commit()
            return self._redirect(f"/vehicle/{row['vehicle_id']}" if row else "/")

        if len(parts) == 3 and parts[0] == "entry" and parts[1].isdigit() and parts[2:] == ["restore"]:
            entry_id = int(parts[1])
            row = conn.execute("SELECT vehicle_id FROM entries WHERE id=?", (entry_id,)).fetchone()
            conn.execute("UPDATE entries SET deleted_at=NULL WHERE id=?", (entry_id,))
            conn.commit()
            return self._redirect(f"/vehicle/{row['vehicle_id']}/trash" if row else "/")

        if len(parts) == 3 and parts[0] == "entry" and parts[1].isdigit() and parts[2:] == ["delete-forever"]:
            entry_id = int(parts[1])
            row = conn.execute("SELECT vehicle_id FROM entries WHERE id=?", (entry_id,)).fetchone()
            conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
            conn.commit()
            return self._redirect(f"/vehicle/{row['vehicle_id']}/trash" if row else "/")

        if len(parts) == 4 and parts[0] == "vehicle" and parts[1].isdigit() and parts[2:] == ["trash", "empty"]:
            vehicle_id = int(parts[1])
            conn.execute("DELETE FROM entries WHERE vehicle_id=? AND deleted_at IS NOT NULL", (vehicle_id,))
            conn.commit()
            return self._redirect(f"/vehicle/{vehicle_id}/trash")

        self._not_found()


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Vehicle Maintenance Record listening on 0.0.0.0:{PORT}")
    print(f"Database: {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
