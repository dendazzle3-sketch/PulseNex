# PulseNex — Website Availability Monitor for Linux

A lightweight, real-time website/API uptime monitor. Checks a list of URLs
on a schedule, tracks response time, HTTP status, SSL certificate expiry,
and optional keyword content — all shown on a live terminal dashboard and
persisted to a local SQLite database.

No agents. No external services. Single Python file.

---

## 1. Requirements

- Linux (or any OS with Python 3 — primarily built/tested for Linux servers)
- Python 3.8+
- `requests` and `rich` Python packages

## 2. Installation

```bash
# 1. Copy pulsenex.py to your machine
mkdir -p ~/pulsenex
cp pulsenex.py ~/pulsenex/
cd ~/pulsenex

# 2. Install dependencies
pip install requests rich --break-system-packages
# or, inside a virtualenv:
python3 -m venv venv
source venv/bin/activate
pip install requests rich

# 3. Make it executable (optional)
chmod +x pulsenex.py
```

### Optional: install as a system-wide command

```bash
sudo cp pulsenex.py /usr/local/bin/pulsenex
sudo chmod +x /usr/local/bin/pulsenex
# now you can run: pulsenex monitor --demo
```

---

## 3. Quick Start (no configuration needed)

Try it instantly with simulated sites and a scripted outage/SSL-warning —
no real sites required:

```bash
python3 pulsenex.py monitor --demo
```

Press `Ctrl+C` to stop.

---

## 4. Monitoring Real Sites

### Add a site

```bash
python3 pulsenex.py add https://example.com --name "My Website"
```

### Add a site with a required keyword and custom interval

```bash
python3 pulsenex.py add https://example.com/dashboard \
  --name "Client Dashboard" \
  --keyword "Welcome back" \
  --interval 30
```

- `--name` — friendly display name (defaults to the domain)
- `--keyword` — text that must appear in the page body; if missing, a WARNING is raised even though the HTTP status is 200
- `--interval` — how often (in seconds) this specific site is checked (default: 60)

### List configured sites

```bash
python3 pulsenex.py list
```

### Remove a site

```bash
python3 pulsenex.py remove https://example.com
# or by ID:
python3 pulsenex.py remove 3
```

### Start monitoring

```bash
python3 pulsenex.py monitor
```

---

## 5. Commands Reference

| Command | Description |
|---|---|
| `pulsenex add <url>` | Add a site to monitor |
| `pulsenex add <url> --name <n> --keyword <k> --interval <s>` | Add a site with options |
| `pulsenex remove <url\|id>` | Remove a monitored site |
| `pulsenex list` | List all configured sites |
| `pulsenex monitor` | Start the live dashboard against configured sites |
| `pulsenex monitor --demo` | Start the live dashboard with simulated sites/incidents |
| `pulsenex report` | Print an uptime/incident summary for the last 24 hours |
| `pulsenex report --hours <N>` | Summarize alerts and uptime over the last N hours |
| `pulsenex report --output <file>` | Write the report to a file instead of the terminal |
| `pulsenex stats` | Show all-time alert statistics |
| `pulsenex clear` | Delete the local database (sites, checks, alerts) |
| `pulsenex clear -y` | Delete without confirmation |
| `pulsenex --version` | Print the tool version |
| `pulsenex <command> --help` | Show help for any command |

### Examples

```bash
# Add three sites
python3 pulsenex.py add https://mysite.com --name "Main Site"
python3 pulsenex.py add https://api.mysite.com/health --name "API" --interval 30
python3 pulsenex.py add https://shop.mysite.com --name "Shop" --keyword "Add to cart"

# Watch them live
python3 pulsenex.py monitor

# Demo dashboard for a client walkthrough / screenshots
python3 pulsenex.py monitor --demo

# Weekly uptime report saved to a file
python3 pulsenex.py report --hours 168 --output weekly_uptime.txt

# All-time stats
python3 pulsenex.py stats
```

---

## 6. Dashboard Layout

```
┌────────────────────────────────────────────────────────────────────┐
│  PulseNex   Website Availability Monitor for Linux  | uptime | v1.0 │
├──────────────────────────────────────────────┬───────────────────────┤
│  Monitored Sites                              │   Status Summary       │
│   Site | State | HTTP | Latency | SSL | Up%   │   Total / UP / DOWN    │
├──────────────────────────────────────────────┤                       │
│  Recent Alerts                                │                       │
│   Time | Sev | Alert                          │                       │
└──────────────────────────────────────────────┴───────────────────────┘
```

- **UP** (green) — site responded successfully
- **DOWN** (red) — connection failure, timeout, or HTTP 5xx
- Alert feed shows outages (CRITICAL), recoveries (INFO), slow responses,
  missing keywords, and SSL expiry warnings (WARNING)

---

## 7. Data Storage

All data is stored locally at:

```
~/.pulsenex/pulsenex.db
```

Query it directly with the `sqlite3` CLI:

```bash
sqlite3 ~/.pulsenex/pulsenex.db "SELECT * FROM sites;"
sqlite3 ~/.pulsenex/pulsenex.db "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT 20;"
sqlite3 ~/.pulsenex/pulsenex.db "SELECT * FROM checks ORDER BY timestamp DESC LIMIT 20;"
```

Schema:

```sql
CREATE TABLE sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    name TEXT,
    keyword TEXT,
    interval_sec INTEGER DEFAULT 60,
    created_at TEXT
);

CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER,
    timestamp TEXT NOT NULL,
    status_code INTEGER,
    response_ms INTEGER,
    success INTEGER,
    error TEXT,
    ssl_days_left INTEGER
);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    severity TEXT NOT NULL,
    site TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL
);
```

---

## 8. Alert Categories (v1.0.0)

| Category | Triggered By | Severity |
|---|---|---|
| `SITE_DOWN` | Connection error, timeout, or HTTP 5xx | CRITICAL |
| `SITE_RECOVERED` | Site returns to a successful response after being down | INFO |
| `SLOW_RESPONSE` | Response time exceeds 2000ms | WARNING |
| `KEYWORD_MISSING` | Configured keyword not found in page body | WARNING |
| `SSL_EXPIRING` | Certificate expires within 14 days | WARNING |
| `SSL_EXPIRED` | Certificate expiry date has passed | CRITICAL |

Thresholds can be changed by editing `SLOW_THRESHOLD_MS` and
`SSL_WARN_DAYS` near the top of `pulsenex.py`.

---

## 9. Running as a Background Service (optional)

```bash
sudo tee /etc/systemd/system/pulsenex.service > /dev/null <<'EOF'
[Unit]
Description=PulseNex Website Availability Monitor
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/pulsenex monitor
Restart=always
User=your-username

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pulsenex
sudo systemctl status pulsenex
```

> Note: `monitor` is an interactive full-screen dashboard. For unattended
> background operation, consider running it inside `tmux`/`screen`, or
> scheduling `report` via cron for periodic email/summary delivery, e.g.:
> `0 * * * * /usr/bin/python3 /usr/local/bin/pulsenex report --hours 1 --output /var/log/pulsenex/hourly.txt`

---

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| `No sites configured` | Add one first: `pulsenex add https://example.com`, or use `--demo` |
| `ModuleNotFoundError: No module named 'requests'` or `'rich'` | `pip install requests rich --break-system-packages` |
| SSL expiry always shows `-` | Site is HTTP (not HTTPS), or the TLS handshake failed (self-signed cert, firewall blocking port 443) |
| Site shows DOWN but works in a browser | Check for a firewall/geo-block on the monitoring server, or increase the timeout in `check_site()` |
| Dashboard looks garbled / misaligned | Resize/maximize your terminal window; Rich adapts to terminal width |
| Want to reset everything | `python3 pulsenex.py clear -y` |

---

## 11. Uninstall

```bash
rm -rf ~/.pulsenex            # removes sites, checks, and alert history
rm /usr/local/bin/pulsenex    # if installed system-wide
```
