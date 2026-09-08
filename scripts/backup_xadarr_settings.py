#!/usr/bin/env python3
"""
Snapshot the shared Xadarr settings blob so a bad push can be undone.

Five TVs, two phones and Episeerr's own web UI all read and write one settings file. A single
device pushing stale state rewrites everyone's favourites, catalogues and server connections in
one call — on 2026-09-05/06 that happened repeatedly, including from a phone nobody remembered
had Xadarr installed, and the Plex server connection vanished more than once. Every recovery had
to be reconstructed by hand from logs, because the only backup on the volume was five months old.

Run hourly from cron. Writes a snapshot only when the content actually changed, so an idle day
costs nothing and a bad change is always one file copy away from being undone.

    backup_xadarr_settings.py              take a snapshot if the content changed
    backup_xadarr_settings.py --list       show snapshots, newest first
    backup_xadarr_settings.py --restore <file>   put one back (writes through Episeerr's API)
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("/home/joe/config/episeerr/data")
SRC = DATA_DIR / "xadarr_settings.json"
DEST = DATA_DIR / "backups"
KEEP = 60
EPISEERR = "http://192.168.254.205:5002"

# updatedAt moves on every push even when nothing meaningful changed, so exclude it from the
# fingerprint. Otherwise this keeps sixty identical copies of an unchanged blob and ages out the
# very state worth recovering.
VOLATILE = ("updatedAt", "lan_sync_last_modified")


def fingerprint(path: Path) -> str:
    data = json.loads(path.read_text())
    for key in VOLATILE:
        data.pop(key, None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def snapshots() -> list[Path]:
    return sorted(DEST.glob("xadarr_settings_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def favourite_count(path: Path) -> str:
    try:
        blob = json.loads(path.read_text())
        return str(len(blob["iptvByProfile"]["default"]["favoriteChannels"]))
    except Exception:
        return "?"


def take_snapshot() -> int:
    if not SRC.exists() or SRC.stat().st_size == 0:
        print(f"source missing or empty: {SRC}", file=sys.stderr)
        return 1

    # Never archive a truncated or half-written file. A corrupt backup is worse than none,
    # because it looks restorable right up until it is needed.
    try:
        current = fingerprint(SRC)
    except (json.JSONDecodeError, OSError) as err:
        print(f"source is not valid JSON, refusing to archive: {err}", file=sys.stderr)
        return 1

    DEST.mkdir(parents=True, exist_ok=True)

    existing = snapshots()
    if existing:
        try:
            if fingerprint(existing[0]) == current:
                return 0  # nothing changed since the last snapshot
        except (json.JSONDecodeError, OSError):
            pass  # unreadable previous snapshot — take a fresh one rather than skip

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = DEST / f"xadarr_settings_{stamp}_{current}.json"
    target.write_bytes(SRC.read_bytes())
    print(f"{datetime.now():%F %T} snapshot {stamp} ({favourite_count(SRC)} favourites)")

    for old in snapshots()[KEEP:]:
        old.unlink(missing_ok=True)
    return 0


def cmd_list() -> int:
    rows = snapshots()
    if not rows:
        print("no snapshots yet")
        return 0
    print(f"{'SNAPSHOT':<44} {'FAVOURITES':>10}  TAKEN")
    for path in rows:
        taken = datetime.fromtimestamp(path.stat().st_mtime).strftime("%F %H:%M")
        print(f"{path.name:<44} {favourite_count(path):>10}  {taken}")
    return 0


def cmd_restore(name: str) -> int:
    """Restore through Episeerr's API rather than by writing the file directly.

    Writing the file straight back leaves `updatedAt` older than what the devices hold, so the
    next TV to open would simply push its own copy over the restore — which is exactly how the
    original loss kept repeating. Going through the API stamps it as the newest change.
    """
    import urllib.request

    path = Path(name)
    if not path.is_absolute():
        path = DEST / name
    if not path.exists():
        print(f"no such snapshot: {path}", file=sys.stderr)
        return 1

    blob = json.loads(path.read_text())
    now = int(time.time() * 1000)
    # Beat both the current blob and any device clock skew — devices here run a couple of
    # minutes ahead, and a restore stamped with local time would lose to them.
    try:
        with urllib.request.urlopen(f"{EPISEERR}/api/integration/xadarr/settings", timeout=15) as resp:
            live = json.loads(resp.read())
        now = max(now, int(live.get("updatedAt") or 0) + 1)
    except Exception:
        pass
    blob["updatedAt"] = now
    blob["lan_sync_last_modified"] = now

    req = urllib.request.Request(
        f"{EPISEERR}/api/integration/xadarr/settings",
        data=json.dumps(blob).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"restored {path.name} -> HTTP {resp.status} ({favourite_count(path)} favourites)")
    print("Open Xadarr on each device to pull it; a device already running keeps its own copy "
          "until it restarts.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return take_snapshot()
    if args[0] == "--list":
        return cmd_list()
    if args[0] == "--restore" and len(args) > 1:
        return cmd_restore(args[1])
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
