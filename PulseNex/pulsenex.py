#!/usr/bin/env python3
"""
PulseNex - Website Availability Monitor for Linux
Author: Afsa Taj

A lightweight, real-time website/uptime monitor for Linux. Periodically
checks a list of URLs over HTTP(S), measures response time, verifies
SSL certificate expiry, optionally checks for a required keyword in the
response body, and renders a live terminal dashboard using Rich. Every
check and every state-change alert (site down, slow, SSL expiring, back up)
is persisted to a local SQLite database.

No external services required. No agents. Single Python file.
"""

import argparse
import os
import ssl
import socket
import sqlite3
import sys
import time
import random
import signal
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.console import Console
from rich.align import Align
from rich import box

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_NAME = "PulseNex"
APP_TAGLINE = "Website Availability Monitor for Linux"
VERSION = "1.0.0"

HOME_DIR = Path.home() / ".pulsenex"
DB_PATH = HOME_DIR / "pulsenex.db"

DEFAULT_CHECK_INTERVAL = 60      # seconds between checks, per site
DEFAULT_TICK = 5                 # scheduler tick — how often we scan for due sites
DEFAULT_TIMEOUT = 10              # seconds, HTTP request timeout
SLOW_THRESHOLD_MS = 2000          # response time above this => WARNING
SSL_WARN_DAYS = 14                # SSL cert expiring within this many days => WARNING

SEVERITY_COLOR = {"CRITICAL": "bold red", "WARNING": "bold yellow", "INFO": "cyan", "OK": "bold green"}

console = Console()

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def init_db():
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            name TEXT,
            keyword TEXT,
            interval_sec INTEGER DEFAULT 60,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            timestamp TEXT NOT NULL,
            status_code INTEGER,
            response_ms INTEGER,
            success INTEGER,
            error TEXT,
            ssl_days_left INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            severity TEXT NOT NULL,
            site TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def add_site(conn, url, name=None, keyword=None, interval=DEFAULT_CHECK_INTERVAL):
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    name = name or urlparse(url).netloc
    conn.execute(
        "INSERT OR IGNORE INTO sites (url, name, keyword, interval_sec, created_at) VALUES (?, ?, ?, ?, ?)",
        (url, name, keyword, interval, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def remove_site(conn, identifier):
    conn.execute("DELETE FROM sites WHERE url = ? OR id = ?", (identifier, identifier))
    conn.commit()


def list_sites(conn):
    return conn.execute("SELECT id, url, name, keyword, interval_sec FROM sites ORDER BY id").fetchall()


def save_check(conn, site_id, result):
    conn.execute(
        "INSERT INTO checks (site_id, timestamp, status_code, response_ms, success, error, ssl_days_left) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            site_id, datetime.now().isoformat(timespec="seconds"), result.get("status_code"),
            result.get("response_ms"), int(result.get("success", False)), result.get("error", ""),
            result.get("ssl_days_left"),
        ),
    )
    conn.commit()


def save_alert(conn, severity, site, category, message):
    conn.execute(
        "INSERT INTO alerts (timestamp, severity, site, category, message) VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(timespec="seconds"), severity, site, category, message),
    )
    conn.commit()

# --------------------------------------------------------------------------
# Checking Logic
# --------------------------------------------------------------------------

def get_ssl_days_left(hostname, port=443, timeout=5):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
        expiry = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        return (expiry - datetime.utcnow()).days
    except Exception:
        return None


def check_site(site):
    """site: dict with id, url, name, keyword. Returns a result dict."""
    url = site["url"]
    parsed = urlparse(url)
    result = {"success": False, "status_code": None, "response_ms": None, "error": "", "ssl_days_left": None}

    start = time.time()
    try:
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
        elapsed_ms = int((time.time() - start) * 1000)
        result["response_ms"] = elapsed_ms
        result["status_code"] = resp.status_code
        result["success"] = resp.status_code < 400

        if site.get("keyword") and site["keyword"] not in resp.text:
            result["keyword_missing"] = True

    except requests.exceptions.SSLError as e:
        result["error"] = f"SSL error: {e}"
    except requests.exceptions.Timeout:
        result["error"] = f"Timeout after {DEFAULT_TIMEOUT}s"
    except requests.exceptions.ConnectionError:
        result["error"] = "Connection failed (host unreachable / DNS failure)"
    except requests.exceptions.RequestException as e:
        result["error"] = str(e)[:150]

    if parsed.scheme == "https":
        result["ssl_days_left"] = get_ssl_days_left(parsed.netloc.split(":")[0])

    return result


def evaluate_result(site, result, previous_state):
    """Turns a check result into a (state, alerts[]) tuple. previous_state is the last known state string."""
    alerts = []
    name = site["name"]

    if not result["success"]:
        state = "DOWN"
        msg = result["error"] or f"HTTP {result['status_code']}"
        if previous_state != "DOWN":
            alerts.append(("CRITICAL", "SITE_DOWN", f"{name} is DOWN — {msg}"))
    else:
        state = "UP"
        if previous_state == "DOWN":
            alerts.append(("INFO", "SITE_RECOVERED", f"{name} is back UP (HTTP {result['status_code']})"))

        if result["response_ms"] and result["response_ms"] > SLOW_THRESHOLD_MS:
            alerts.append(("WARNING", "SLOW_RESPONSE", f"{name} responded slowly: {result['response_ms']}ms"))

        if result.get("keyword_missing"):
            alerts.append(("WARNING", "KEYWORD_MISSING", f"{name} is up but expected keyword was not found on the page"))

        if result["ssl_days_left"] is not None and 0 <= result["ssl_days_left"] <= SSL_WARN_DAYS:
            alerts.append(("WARNING", "SSL_EXPIRING", f"{name} SSL certificate expires in {result['ssl_days_left']} day(s)"))
        elif result["ssl_days_left"] is not None and result["ssl_days_left"] < 0:
            alerts.append(("CRITICAL", "SSL_EXPIRED", f"{name} SSL certificate has EXPIRED"))

    return state, alerts

# --------------------------------------------------------------------------
# Demo Mode
# --------------------------------------------------------------------------

DEMO_SITES = [
    {"id": 1, "url": "https://nexuswebsecurity.com", "name": "nexuswebsecurity.com", "keyword": None},
    {"id": 2, "url": "https://client-portal.example.com", "name": "client-portal.example.com", "keyword": "Dashboard"},
    {"id": 3, "url": "https://shop.example.com", "name": "shop.example.com", "keyword": None},
    {"id": 4, "url": "https://api.example.com/health", "name": "api.example.com", "keyword": None},
]

def demo_check(site, tick):
    """Generates a synthetic but realistic result for demo purposes."""
    roll = random.random()
    result = {"success": True, "status_code": 200, "response_ms": random.randint(80, 400),
               "error": "", "ssl_days_left": random.randint(20, 300)}

    # Inject occasional incidents so the dashboard looks alive
    if site["id"] == 3 and tick % 9 == 0:
        result.update(success=False, status_code=None, response_ms=None, error="Connection failed (host unreachable / DNS failure)")
    elif site["id"] == 2 and tick % 6 == 0:
        result.update(response_ms=random.randint(2200, 4200))
    elif site["id"] == 4 and tick % 11 == 0:
        result.update(success=False, status_code=503, response_ms=random.randint(3000, 5000), error="")
    elif site["id"] == 1 and tick == 3:
        result["ssl_days_left"] = 9

    return result

# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

class Dashboard:
    def __init__(self, sites):
        self.sites = {s["id"]: s for s in sites}
        self.status = {s["id"]: {"state": "UNKNOWN", "response_ms": None, "status_code": None,
                                   "last_checked": None, "ssl_days_left": None, "checks": 0, "up_checks": 0}
                        for s in sites}
        self.recent_alerts = deque(maxlen=12)
        self.start_time = datetime.now()

    def update_site(self, site_id, result, state):
        s = self.status[site_id]
        s["state"] = state
        s["response_ms"] = result.get("response_ms")
        s["status_code"] = result.get("status_code")
        s["ssl_days_left"] = result.get("ssl_days_left")
        s["last_checked"] = datetime.now().strftime("%H:%M:%S")
        s["checks"] += 1
        if state == "UP":
            s["up_checks"] += 1

    def add_alert(self, severity, site_name, category, message):
        self.recent_alerts.appendleft({
            "time": datetime.now().strftime("%H:%M:%S"),
            "severity": severity, "site": site_name, "category": category, "message": message,
        })

    def render_header(self):
        uptime = str(datetime.now() - self.start_time).split(".")[0]
        title = Text()
        title.append(f" {APP_NAME} ", style="bold white on dark_green")
        title.append(f"  {APP_TAGLINE}  ", style="bold white")
        title.append(f"| uptime {uptime} | v{VERSION} | {len(self.sites)} site(s)", style="dim")
        return Panel(Align.left(title), box=box.HEAVY, style="on grey11")

    def render_sites_table(self):
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        table.add_column("Site", ratio=3)
        table.add_column("State", width=9)
        table.add_column("HTTP", width=6, justify="center")
        table.add_column("Latency", width=9, justify="right")
        table.add_column("SSL Exp.", width=9, justify="right")
        table.add_column("Uptime", width=8, justify="right")
        table.add_column("Last Check", width=10)

        for site_id, site in self.sites.items():
            s = self.status[site_id]
            state = s["state"]
            style = {"UP": "bold green", "DOWN": "bold red", "UNKNOWN": "dim"}.get(state, "white")
            latency = f"{s['response_ms']}ms" if s["response_ms"] is not None else "-"
            httpcode = str(s["status_code"]) if s["status_code"] else "-"
            ssl_txt = f"{s['ssl_days_left']}d" if s["ssl_days_left"] is not None else "-"
            if s["ssl_days_left"] is not None and s["ssl_days_left"] <= SSL_WARN_DAYS:
                ssl_txt = f"[bold yellow]{ssl_txt}[/]"
            uptime_pct = f"{(s['up_checks'] / s['checks'] * 100):.1f}%" if s["checks"] else "-"
            table.add_row(
                site["name"], f"[{style}]{state}[/]", httpcode, latency, ssl_txt, uptime_pct,
                s["last_checked"] or "-",
            )
        return Panel(table, title="Monitored Sites", border_style="green")

    def render_summary(self):
        total = len(self.sites)
        up = sum(1 for s in self.status.values() if s["state"] == "UP")
        down = sum(1 for s in self.status.values() if s["state"] == "DOWN")
        unknown = total - up - down
        table = Table(box=box.SIMPLE, show_header=False, expand=True)
        table.add_column("k", style="bold")
        table.add_column("v", justify="right")
        table.add_row("Total Sites", str(total))
        table.add_row("[bold green]UP[/]", str(up))
        table.add_row("[bold red]DOWN[/]", str(down))
        table.add_row("Pending", str(unknown))
        return Panel(table, title="Status Summary", border_style="blue")

    def render_alerts(self):
        table = Table(box=box.SIMPLE, expand=True)
        table.add_column("Time", width=8)
        table.add_column("Sev", width=8)
        table.add_column("Alert")
        for a in self.recent_alerts:
            sev_style = SEVERITY_COLOR.get(a["severity"], "white")
            table.add_row(a["time"], f"[{sev_style}]{a['severity']}[/]", f"{a['site']}: {a['message']}")
        if not self.recent_alerts:
            table.add_row("-", "-", "No alerts yet")
        return Panel(table, title="Recent Alerts", border_style="magenta")

    def render(self):
        layout = Layout()
        layout.split_column(
            Layout(self.render_header(), size=3),
            Layout(name="body"),
        )
        layout["body"].split_row(
            Layout(name="main", ratio=3),
            Layout(name="side", ratio=1),
        )
        layout["main"].split_column(
            Layout(self.render_sites_table(), ratio=2),
            Layout(self.render_alerts(), ratio=2),
        )
        layout["side"].update(self.render_summary())
        return layout

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_add(args):
    conn = init_db()
    add_site(conn, args.url, name=args.name, keyword=args.keyword, interval=args.interval)
    console.print(f"[bold green]Added site:[/] {args.url}")


def cmd_remove(args):
    conn = init_db()
    remove_site(conn, args.identifier)
    console.print(f"[bold green]Removed site (if it existed):[/] {args.identifier}")


def cmd_list(args):
    conn = init_db()
    sites = list_sites(conn)
    if not sites:
        console.print("No sites configured yet. Add one with: pulsenex add <url>")
        return
    table = Table(title=f"{APP_NAME} - Monitored Sites", box=box.ROUNDED)
    table.add_column("ID")
    table.add_column("URL")
    table.add_column("Name")
    table.add_column("Keyword")
    table.add_column("Interval (s)")
    for sid, url, name, keyword, interval in sites:
        table.add_row(str(sid), url, name or "-", keyword or "-", str(interval))
    console.print(table)


def cmd_monitor(args):
    conn = init_db()

    if args.demo:
        sites = DEMO_SITES
    else:
        rows = list_sites(conn)
        if not rows:
            console.print(
                "[bold red]No sites configured.[/] Add one first: "
                "pulsenex add https://example.com\nOr try: pulsenex monitor --demo"
            )
            sys.exit(1)
        sites = [{"id": r[0], "url": r[1], "name": r[2], "keyword": r[3], "interval": r[4]} for r in rows]

    dashboard = Dashboard(sites)
    last_checked = {s["id"]: 0 for s in sites}
    tick_count = 0

    def handle_sigint(sig, frame):
        console.print(f"\n[bold cyan]{APP_NAME} stopped. Data saved to[/] {DB_PATH}")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    with Live(dashboard.render(), refresh_per_second=4, screen=True) as live:
        while True:
            now = time.time()
            tick_count += 1
            for site in sites:
                interval = site.get("interval", args.interval) or args.interval
                if args.demo:
                    interval = args.interval  # move faster in demo mode
                if now - last_checked[site["id"]] < interval:
                    continue
                last_checked[site["id"]] = now

                if args.demo:
                    result = demo_check(site, tick_count)
                else:
                    result = check_site(site)

                prev_state = dashboard.status[site["id"]]["state"]
                state, alerts = evaluate_result(site, result, prev_state)
                dashboard.update_site(site["id"], result, state)

                if not args.demo:
                    save_check(conn, site["id"], result)
                for severity, category, message in alerts:
                    dashboard.add_alert(severity, site["name"], category, message)
                    if not args.demo:
                        save_alert(conn, severity, site["name"], category, message)

                live.update(dashboard.render())

            time.sleep(1)


def cmd_report(args):
    conn = init_db()
    since = datetime.now() - timedelta(hours=args.hours)
    cur = conn.execute(
        "SELECT timestamp, severity, site, category, message FROM alerts "
        "WHERE timestamp >= ? ORDER BY timestamp DESC",
        (since.isoformat(timespec="seconds"),),
    )
    rows = cur.fetchall()

    lines = []
    lines.append(f"{APP_NAME} Availability Report")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Window: last {args.hours} hour(s)")
    lines.append(f"Total alerts: {len(rows)}")
    lines.append("-" * 70)

    for r in rows:
        ts, sev, site, cat, msg = r
        lines.append(f"[{ts}] {sev:<8} {site:<28} {cat:<16} {msg}")

    # Per-site uptime summary
    lines.append("-" * 70)
    lines.append("Per-site uptime (based on stored checks in window):")
    sites = list_sites(conn)
    for sid, url, name, keyword, interval in sites:
        cur2 = conn.execute(
            "SELECT COUNT(*), SUM(success) FROM checks WHERE site_id = ? AND timestamp >= ?",
            (sid, since.isoformat(timespec="seconds")),
        )
        total, up = cur2.fetchone()
        total = total or 0
        up = up or 0
        pct = (up / total * 100) if total else 0
        lines.append(f"  {name:<28} {up}/{total} checks up  ({pct:.1f}% uptime)")

    output_text = "\n".join(lines)

    if args.output:
        Path(args.output).write_text(output_text)
        console.print(f"[bold green]Report written to {args.output}[/]")
    else:
        console.print(output_text)


def cmd_stats(args):
    conn = init_db()
    cur = conn.execute("SELECT severity, COUNT(*) FROM alerts GROUP BY severity")
    sev_rows = dict(cur.fetchall())
    cur = conn.execute("SELECT site, COUNT(*) c FROM alerts GROUP BY site ORDER BY c DESC LIMIT 10")
    top_sites = cur.fetchall()
    cur = conn.execute("SELECT category, COUNT(*) c FROM alerts GROUP BY category ORDER BY c DESC LIMIT 10")
    top_cats = cur.fetchall()

    table = Table(title=f"{APP_NAME} - All-Time Alert Severity", box=box.ROUNDED)
    table.add_column("Severity")
    table.add_column("Count", justify="right")
    for sev in ("CRITICAL", "WARNING", "INFO"):
        table.add_row(sev, str(sev_rows.get(sev, 0)))
    console.print(table)

    table2 = Table(title="Sites with Most Alerts", box=box.ROUNDED)
    table2.add_column("Site")
    table2.add_column("Alerts", justify="right")
    for site, c in top_sites:
        table2.add_row(site, str(c))
    console.print(table2)

    table3 = Table(title="Alert Categories", box=box.ROUNDED)
    table3.add_column("Category")
    table3.add_column("Count", justify="right")
    for cat, c in top_cats:
        table3.add_row(cat, str(c))
    console.print(table3)


def cmd_clear(args):
    if DB_PATH.exists():
        if not args.yes:
            confirm = input(f"This will permanently delete {DB_PATH} (sites, checks, alerts). Type 'yes' to confirm: ")
            if confirm.strip().lower() != "yes":
                console.print("Cancelled.")
                return
        DB_PATH.unlink()
        console.print("[bold green]PulseNex database cleared.[/]")
    else:
        console.print("No database found.")

# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="pulsenex",
        description=f"{APP_NAME} - {APP_TAGLINE}",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add a site to monitor")
    p_add.add_argument("url", help="Site URL, e.g. https://example.com")
    p_add.add_argument("--name", help="Friendly display name")
    p_add.add_argument("--keyword", help="Require this text to be present in the page body")
    p_add.add_argument("--interval", type=int, default=DEFAULT_CHECK_INTERVAL, help="Check interval in seconds (default: 60)")
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="Remove a monitored site (by URL or ID)")
    p_remove.add_argument("identifier", help="Site URL or numeric ID")
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="List all monitored sites")
    p_list.set_defaults(func=cmd_list)

    p_monitor = sub.add_parser("monitor", help="Start the live monitoring dashboard")
    p_monitor.add_argument("--interval", type=int, default=DEFAULT_TICK,
                            help="Fallback / demo check interval in seconds (default: 5)")
    p_monitor.add_argument("--demo", action="store_true", help="Run with simulated sites and incidents")
    p_monitor.set_defaults(func=cmd_monitor)

    p_report = sub.add_parser("report", help="Generate a text summary report from stored alerts")
    p_report.add_argument("--hours", type=int, default=24, help="Look back this many hours (default: 24)")
    p_report.add_argument("--output", help="Write report to this file instead of stdout")
    p_report.set_defaults(func=cmd_report)

    p_stats = sub.add_parser("stats", help="Show all-time alert statistics")
    p_stats.set_defaults(func=cmd_stats)

    p_clear = sub.add_parser("clear", help="Delete the local database (sites, checks, alerts)")
    p_clear.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_clear.set_defaults(func=cmd_clear)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
