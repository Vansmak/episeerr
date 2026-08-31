#!/usr/bin/env python3
"""
Watches Dispatcharr's upstream providers and says something when one dies.

Written after a Titan outage on 2026-08-29 went unnoticed for ~35 hours: their CDN hostname
stopped resolving (the provider rotated to a new XC URL), which killed both the M3U and the EPG
feed. The only symptom was gradually emptying guide data and channels that wouldn't play, and by
the time it was obvious, 497 of 765 channels were dead. Dispatcharr records the failure in its own
tables the moment it happens — nothing was watching them.

Checks, cheapest first:
  1. M3U accounts and EPG sources whose status is `error`
  2. DNS for each active source host — NXDOMAIN means the provider moved, not a blip, and is the
     signature of the rotation above
  3. EPG coverage — how many channels have no programme covering right now, which is what you
     actually see in the guide

Notifies through Episeerr's generic endpoint, which the Xadarr apps poll, so it needs no
credentials of its own. Fires only on a change of state, so a long outage doesn't nag, and says so
when things recover.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

EPISEERR_URL = os.environ.get("EPISEERR_URL", "http://192.168.254.205:5002")
STATE_FILE = os.path.expanduser("~/.cache/dispatcharr_provider_health.json")
CONTAINER = "dispatcharr"

# Below this many channels with live EPG we assume something upstream broke rather than a few
# channels legitimately having no listings.
EPG_COVERAGE_FLOOR = 0.5

# Enough notice to renew or line up a replacement without a scramble.
EXPIRY_WARN_DAYS = 10


def psql(sql: str) -> list[list[str]]:
    """Runs a query and returns rows of columns. Empty on any failure — a monitor that crashes
    when the thing it monitors is down is worse than useless."""
    try:
        out = subprocess.run(
            ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", "dispatcharr", "-tAF|", "-c", sql],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return []
        return [line.split("|") for line in out.stdout.strip().splitlines() if line.strip()]
    except Exception:
        return []


def host_of(url: str) -> str | None:
    if not url:
        return None
    try:
        return urllib.parse.urlparse(url).hostname
    except Exception:
        return None


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
        return True
    except socket.gaierror:
        return False
    except Exception:
        # Anything else (timeout, transient) is not evidence the domain is gone.
        return True


def expiry_days(url: str, user: str, pw: str) -> int | None:
    """Days until an XC account lapses, via its own player_api. None if unreadable.

    Sends a browser User-Agent because Cloudflare-fronted panels answer Python's default with a
    403 — which would silently skip the expiry check for precisely the providers most prone to
    rotating domains, the failure this whole script exists to catch.
    """
    import urllib.parse as up
    q = up.urlencode({"username": user, "password": pw})
    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/player_api.php?{q}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            info = json.load(r).get("user_info") or {}
        exp = info.get("exp_date")
        if not exp:
            return None
        return round((int(exp) - datetime.now(timezone.utc).timestamp()) / 86400)
    except Exception:
        return None


def collect() -> dict:
    problems: list[str] = []
    detail: list[str] = []

    # Subscriptions lapsing. A provider going dark mid-week is the same outage as a dead domain,
    # just one you can prevent — and the renewal date lives on the provider's side, not in
    # Dispatcharr, so nothing local would ever surface it.
    for _id, name, url, user, pw in psql("""
        SELECT id::text, name, coalesce(server_url,''), coalesce(username,''), coalesce(password,'')
        FROM m3u_m3uaccount WHERE is_active AND account_type='XC' AND server_url <> '';
    """):
        if not (user and pw):
            continue
        days = expiry_days(url, user, pw)
        if days is None:
            continue
        if days < 0:
            problems.append(f"expired:{name}")
            detail.append(f"{name} subscription EXPIRED")
        elif days <= EXPIRY_WARN_DAYS:
            problems.append(f"expiring:{name}:{days}")
            detail.append(f"{name} expires in {days} day{'s' if days != 1 else ''}")

    # 1 + 2: provider sources, their status and whether their host still exists.
    rows = psql("""
        SELECT 'm3u', id::text, name, coalesce(status,''), coalesce(server_url,'')
        FROM m3u_m3uaccount WHERE is_active
        UNION ALL
        SELECT 'epg', id::text, name, coalesce(status,''), coalesce(url,'')
        FROM epg_epgsource WHERE is_active;
    """)
    dead_hosts: set[str] = set()
    for kind, _id, name, status, url in rows:
        host = host_of(url)
        if host and host not in dead_hosts and not resolves(host):
            dead_hosts.add(host)
            problems.append(f"dns:{host}")
            detail.append(f"{name}: host {host} no longer resolves — provider likely moved")
        elif status == "error":
            problems.append(f"{kind}:{name}")
            detail.append(f"{name} ({kind.upper()}) is failing")

    # 3: what the guide actually looks like right now.
    cov = psql("""
        SELECT count(*) FILTER (WHERE p.id IS NOT NULL), count(*)
        FROM dispatcharr_channels_channel c
        LEFT JOIN epg_programdata p
          ON p.epg_id = c.epg_data_id AND now() BETWEEN p.start_time AND p.end_time;
    """)
    if cov and len(cov[0]) == 2:
        try:
            with_epg, total = int(cov[0][0]), int(cov[0][1])
            if total and (with_epg / total) < EPG_COVERAGE_FLOOR:
                problems.append("epg:coverage")
                detail.append(f"only {with_epg} of {total} channels have current guide data")
        except ValueError:
            pass

    return {"problems": sorted(set(problems)), "detail": detail}


def notify(title: str, message: str, kind: str) -> bool:
    body = json.dumps({
        "source": "Dispatcharr", "title": title, "message": message, "type": kind,
    }).encode()
    req = urllib.request.Request(
        f"{EPISEERR_URL}/api/notify", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def main() -> int:
    current = collect()
    if not current["detail"] and not current["problems"]:
        # A totally empty result usually means psql itself failed; don't report "all clear".
        if not psql("SELECT 1;"):
            return 0

    previous: dict = {}
    try:
        with open(STATE_FILE) as fh:
            previous = json.load(fh)
    except Exception:
        pass

    was = set(previous.get("problems", []))
    now = set(current["problems"])
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if now and now != was:
        notify("IPTV provider problem", "; ".join(current["detail"])[:500], "error")
        print(f"[{stamp}] ALERT: {'; '.join(current['detail'])}")
    elif was and not now:
        notify("IPTV providers recovered", "All active sources are resolving and refreshing.", "success")
        print(f"[{stamp}] recovered")
    else:
        print(f"[{stamp}] {'unchanged: ' + ', '.join(sorted(now)) if now else 'ok'}")

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump({"problems": sorted(now), "detail": current["detail"], "checked": stamp}, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
