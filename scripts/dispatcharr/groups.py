#!/usr/bin/env python3
"""
Turn Dispatcharr channel groups on and off without hand-editing SQL.

A group only produces channels when `auto_channel_sync` is set for it on a provider account, which
is per-group *and* per-account and defaults off — so adding a category used to mean a manual DB
edit. This wraps that.

Anything enabled here survives the maintenance script: whitelist enforcement is scoped to the
curated Tier 1 groups, so Tier 2 categories you add are left alone.

    groups.py list [filter]     what exists, and what's on
    groups.py on  <name>...     enable (exact name, or 'Sports | *' to glob)
    groups.py off <name>...     disable and remove that group's channels
    groups.py sync              apply changes — pulls streams, then creates/removes channels

Names are matched case-insensitively; * globs. Always quote names containing | or *.
"""

from __future__ import annotations

import subprocess
import sys

CONTAINER = "dispatcharr"
# The curated lineup. Enforced by the whitelist, numbered by Part 8 — not for casual toggling.
TIER1 = ("Entertainment", "Movies", "News", "Sports", "Documentary", "Locals", "4K", "PPV")


# Group names themselves contain '|' ("US | Sports"), so the field separator has to be something
# that can't appear in the data — a literal pipe splits those names apart mid-value.
SEP = "\x1f"


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", "dispatcharr",
         "-tA", "-F", SEP, "-c", sql],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        print(out.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    return [l.split(SEP) for l in out.stdout.strip().splitlines() if l.strip()]


def sql_literal(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def where_names(names: list[str]) -> str:
    """Group names match exactly, or as a glob when they contain '*'."""
    parts = []
    for n in names:
        if "*" in n:
            parts.append(f"g.name ILIKE {sql_literal(n.replace('*', '%'))}")
        else:
            parts.append(f"LOWER(g.name) = LOWER({sql_literal(n)})")
    return "(" + " OR ".join(parts) + ")"


def cmd_list(filt: str | None) -> None:
    cond = f"AND g.name ILIKE {sql_literal('%' + filt + '%')}" if filt else ""
    rows = psql(f"""
        SELECT g.name,
               COUNT(*) FILTER (WHERE m.auto_channel_sync AND a.is_active) AS on_accounts,
               (SELECT COUNT(*) FROM dispatcharr_channels_channel c WHERE c.channel_group_id = g.id),
               COUNT(DISTINCT s.id)
        FROM dispatcharr_channels_channelgroup g
        LEFT JOIN dispatcharr_channels_channelgroupm3uaccount m ON m.channel_group_id = g.id
        LEFT JOIN m3u_m3uaccount a ON a.id = m.m3u_account_id
        LEFT JOIN dispatcharr_channels_stream s ON s.channel_group_id = g.id
        WHERE TRUE {cond}
        GROUP BY g.id, g.name
        HAVING COUNT(DISTINCT s.id) > 0 OR COUNT(*) FILTER (WHERE m.auto_channel_sync) > 0
        ORDER BY (COUNT(*) FILTER (WHERE m.auto_channel_sync AND a.is_active) > 0) DESC, g.name;
    """)
    if not rows:
        print("no groups matched")
        return
    print(f"{'ON':<4}{'GROUP':<42}{'CHANNELS':>9}{'STREAMS':>9}  TIER")
    for name, on, chans, streams in rows:
        tier = "1" if name in TIER1 else "2"
        print(f"{'●' if int(on) else '·':<4}{name[:41]:<42}{chans:>9}{streams:>9}  {tier}")
    print("\n● = creating channels.  Tier 1 is the curated lineup; Tier 2 is hidden in Xadarr until unhidden.")


def cmd_toggle(names: list[str], enable: bool) -> None:
    cond = where_names(names)
    matched = psql(f"SELECT DISTINCT g.name FROM dispatcharr_channels_channelgroup g WHERE {cond};")
    if not matched:
        print("no groups matched — try: groups.py list <word>")
        sys.exit(1)

    hit_tier1 = [m[0] for m in matched if m[0] in TIER1]
    if hit_tier1 and not enable:
        print(f"refusing: {', '.join(hit_tier1)} is part of the curated lineup.")
        print("Disabling it would empty a numbered group. Edit maintenance.sql if that's really intended.")
        sys.exit(1)

    psql(f"""
        UPDATE dispatcharr_channels_channelgroupm3uaccount m
           SET auto_channel_sync = {str(enable).lower()},
               enabled = TRUE,
               auto_sync_channel_start = COALESCE(m.auto_sync_channel_start, 900)
          FROM dispatcharr_channels_channelgroup g, m3u_m3uaccount a
         WHERE g.id = m.channel_group_id AND a.id = m.m3u_account_id
           AND a.is_active AND {cond};
    """)
    for m in matched:
        print(("enabled " if enable else "disabled ") + m[0])

    if not enable:
        # Channels for a disabled group would otherwise linger with no way to refresh them.
        n = psql(f"""
            WITH d AS (
              DELETE FROM dispatcharr_channels_channel c
               USING dispatcharr_channels_channelgroup g
               WHERE g.id = c.channel_group_id AND c.auto_created AND {cond}
              RETURNING 1)
            SELECT COUNT(*) FROM d;
        """)
        print(f"removed {n[0][0]} channels")
    print("\nnow run:  groups.py sync")


def cmd_sync() -> None:
    accounts = psql("SELECT id, name FROM m3u_m3uaccount WHERE is_active AND account_type='XC' ORDER BY id;")
    ids = ", ".join(a[0] for a in accounts)
    print("syncing:", ", ".join(a[1] for a in accounts))
    subprocess.run([
        "docker", "exec", CONTAINER, "python", "manage.py", "shell", "-c",
        # Full account refresh, not sync_auto_channels: the latter is a no-op when the provider's
        # streams haven't changed, so a group you just switched on never gets its channels built.
        f"from apps.m3u.tasks import refresh_single_m3u_account\n"
        f"for i in [{ids}]: refresh_single_m3u_account.delay(i)\n"
        f"print('queued')",
    ], capture_output=True, text=True, timeout=120)
    print("queued. Channels appear within a few minutes; the maintenance script then merges")
    print("duplicates across providers, orders streams best-quality-first, and renumbers.")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "list":
        cmd_list(args[0] if args else None)
    elif cmd in ("on", "off"):
        if not args:
            print(f"usage: groups.py {cmd} <group name>")
            return 1
        cmd_toggle(args, cmd == "on")
    elif cmd == "sync":
        cmd_sync()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
