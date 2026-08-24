# Vehicle Maintenance Record

A small, dependency-free Python HTTP server that tracks vehicle service
history and tells you what's overdue. No pip installs, no framework, no
database server — one file, the standard library, and a SQLite file next to
it. Pairs with [Garage](https://github.com/ned777/vehicle-maintenance), a
thin Android WebView app that points at this server.

## Features

- **One or more vehicles**, each with its own odometer reading and full
  service history.
- **A reminder for every service type**, computed from your own log —
  whichever comes first between a mileage interval and a time interval,
  same as a real owner's manual:

  | Service | Interval |
  |---|---|
  | Oil Change | 5,000 mi / 6 mo |
  | Tire Rotation | 5,000 mi / 6 mo |
  | Engine Air Filter | 15,000 mi / 12 mo |
  | Cabin Air Filter | 15,000 mi / 12 mo |
  | Brake Inspection | 12,000 mi / 12 mo |
  | Brake Fluid Flush | 30,000 mi / 24 mo |
  | Coolant Flush | 30,000 mi / 30 mo |
  | Transmission Fluid | 30,000 mi / 24 mo |
  | Differential Oil | 30,000 mi / 24 mo |
  | Spark Plugs | 60,000 mi |
  | Battery Check | 12 mo |
  | Wiper Blades | 12 mo |

  Each shows as **OK**, **due soon**, **overdue** (with exactly how many
  miles over and since what date), or **no record yet**.
- **One log entry can cover several services at once** — check off Oil
  Change *and* Tire Rotation for the same visit and it's stored as a single
  dated entry, not two.
- **Edit and soft-delete** — deleting moves an entry to a trash view first
  (restore it, or delete forever/empty the whole trash); editing shows a
  confirmation before saving. Both work through a custom modal rather than
  the browser's native `confirm()`, which Android's WebView never actually
  implements.
- **Paginated history** at 100 entries per vehicle per page — reminders
  always look at the *complete* history regardless of which page you're on.
- **Responsive**: a proper wide layout on desktop, a stacked card layout on
  phones (including inside the Android app's WebView) — no shared table grid
  to misalign between the two.

## Running it

Needs nothing but Python 3 — no `pip install`, no virtualenv.

1. Copy `local_secrets.py.example` to `local_secrets.py` and set your own
   `AUTH_USER`/`AUTH_PASS`. This file is gitignored; the server refuses to
   start with the real credentials committed anywhere.
2. Run it:
   ```sh
   python3 server.py
   ```
   Serves on `0.0.0.0:8091`, protected by HTTP Basic Auth.

It's designed to run as a small always-on service on a home server. Example
`systemd --user` unit (no root needed):

```ini
# ~/.config/systemd/user/vmr.service
[Unit]
Description=Vehicle Maintenance Record server
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/this/repo
ExecStart=/usr/bin/python3 /path/to/this/repo/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```sh
loginctl enable-linger $USER   # so it survives logout, once
systemctl --user daemon-reload
systemctl --user enable --now vmr.service
```

## Data

Everything lives in `maintenance.db`, a SQLite file created next to
`server.py` on first run — gitignored, since it's your actual data, not
part of the app.

## Why no framework

Same philosophy as this author's other self-hosted tools
([sysmon-widget](https://github.com/ned777/sysmon-widget)'s agent, for
one): a single Python file using only `http.server` and `sqlite3` has
nothing to `pip install`, nothing to break on a Python or dependency
update, and nothing to patch for a framework CVE. For a personal,
single-user tool on a home network, that trade-off is worth more than the
convenience a framework would add.
