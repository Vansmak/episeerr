#!/usr/bin/env python3
"""
Add a channel from the provider catalogue to the curated lineup, by hand.

The providers carry ~16,000 streams; the curated lineup is a few hundred. Pulling one specific
channel across — a local station for one game, a network you've just decided you want — otherwise
means either enabling a whole group or writing the INSERT yourself, and that INSERT needs four
not-null columns with no defaults, the right stream order, and a channel-profile row or the channel
never reaches the TV at all.

    channel.py find wthr                 search every provider stream
    channel.py add NBCWTHR.us            add it (auto-picks a free number in its group's range)
    channel.py add NBCWTHR.us --num 760  or choose the number
    channel.py drop NBCWTHR.us           remove it again

Adding attaches every active provider's copy, ordered by that account's priority, so the channel
gets failover for free. Nothing here touches maintenance.sql — but note that Part 6 deletes
auto_created channels whose tvg_id isn't whitelisted, so a channel added this way disappears on the
next maintenance run unless you also add its tvg_id to the whitelist. `add` warns when that applies
and `--whitelist` does it for you.
"""

from __future__ import annotations

import re
import subprocess
import sys

CONTAINER = "dispatcharr"
SQL_PATH = "/home/joe/projects/episeerr_custom/scripts/dispatcharr/maintenance.sql"
SEP = "\x1f"

# Where Part 8 numbers each curated group, so an added channel lands somewhere sensible.
RANGES = {
    "News": (101, 199), "Sports": (201, 299), "Documentary": (301, 399),
    "Entertainment": (401, 599), "Movies": (601, 699), "Locals": (700, 799),
    "4K": (801, 899), "PPV": (901, 1099),
}


def psql(sql: str, quiet: bool = False) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", "dispatcharr",
         "-tA", "-F", SEP, "-c", sql],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        if not quiet:
            print(out.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    return [l.split(SEP) for l in out.stdout.strip().splitlines() if l.strip()]


def lit(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def whitelisted(tvg_id: str) -> bool:
    try:
        sql = open(SQL_PATH).read()
    except OSError:
        return False
    m = re.search(r"INSERT INTO _approved_tvgids \(tvg_id\) VALUES(.*?);", sql, re.S)
    if not m:
        return False
    return tvg_id.lower() in {x.lower() for x in re.findall(r"'([^']+)'", m.group(1))}


def add_to_whitelist(tvg_id: str) -> bool:
    sql = open(SQL_PATH).read()
    m = re.search(r"(INSERT INTO _approved_tvgids \(tvg_id\) VALUES)(.*?)(;)", sql, re.S)
    if not m or whitelisted(tvg_id):
        return False
    sql = sql[:m.end(2)] + ",\n  ('%s')" % tvg_id + sql[m.end(2):]
    open(SQL_PATH, "w").write(sql)
    return True


def cmd_find(term: str) -> None:
    rows = psql(f"""
        SELECT DISTINCT coalesce(s.tvg_id,''), s.name, coalesce(g.name,''), a.name
        FROM dispatcharr_channels_stream s
        JOIN m3u_m3uaccount a ON a.id = s.m3u_account_id AND a.is_active
        LEFT JOIN dispatcharr_channels_channelgroup g ON g.id = s.channel_group_id
        WHERE s.name ILIKE {lit('%' + term + '%')} OR s.tvg_id ILIKE {lit('%' + term + '%')}
        ORDER BY 2 LIMIT 40;
    """)
    if not rows:
        print("nothing matched")
        return
    # One line per distinct channel; providers carrying it are collapsed onto that line.
    merged: dict = {}
    for tvg, name, grp, prov in rows:
        key = tvg.lower() or name.lower()
        e = merged.setdefault(key, {"tvg": tvg, "name": name, "grp": grp, "provs": set()})
        e["provs"].add(prov)
    print(f"{'TVG_ID':<28}{'NAME':<40}{'GROUP':<22}PROVIDERS")
    for e in merged.values():
        print(f"{(e['tvg'] or '—'):<28}{e['name'][:39]:<40}{e['grp'][:21]:<22}{','.join(sorted(e['provs']))}")
    print("\nAdd one with:  channel.py add <TVG_ID>")


def pick_number(group: str) -> int:
    lo, hi = RANGES.get(group, (2000, 2999))
    used = {int(float(r[0])) for r in psql(
        f"SELECT channel_number FROM dispatcharr_channels_channel "
        f"WHERE channel_number BETWEEN {lo} AND {hi};") if r[0]}
    for n in range(lo, hi + 1):
        if n not in used:
            return n
    raise SystemExit(f"no free channel number in {group}'s range ({lo}-{hi})")


def cmd_add(tvg_id: str, num: int | None, group: str | None, do_whitelist: bool) -> None:
    streams = psql(f"""
        SELECT s.id, s.name, a.name, coalesce(g.name,'')
        FROM dispatcharr_channels_stream s
        JOIN m3u_m3uaccount a ON a.id = s.m3u_account_id AND a.is_active
        LEFT JOIN dispatcharr_channels_channelgroup g ON g.id = s.channel_group_id
        WHERE lower(s.tvg_id) = lower({lit(tvg_id)});
    """)
    if not streams:
        print(f"no active provider carries {tvg_id} — try: channel.py find <name>")
        sys.exit(1)

    if psql(f"SELECT 1 FROM dispatcharr_channels_channel WHERE lower(tvg_id)=lower({lit(tvg_id)});", quiet=True):
        print(f"{tvg_id} is already in the lineup")
        sys.exit(0)

    src = streams[0][3]
    if not group:
        # Locals stay Locals; everything else lands in the group its source name suggests.
        group = ("Locals" if "local" in src.lower() else
                 "Sports" if "sport" in src.lower() else
                 "News" if "news" in src.lower() else
                 "Movies" if "movie" in src.lower() else "Entertainment")
    number = num or pick_number(group)
    name = re.sub(r"^(US[:|]\s*|IN\s*\|\s*)", "", streams[0][1]).strip()

    psql(f"""
        INSERT INTO dispatcharr_channels_channel
          (channel_number, name, tvg_id, uuid, auto_created, channel_group_id, epg_data_id,
           user_level, created_at, updated_at, is_adult, hidden_from_output, is_catchup, catchup_days)
        SELECT {number}, {lit(name)}, {lit(tvg_id)}, gen_random_uuid(), true,
               (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = {lit(group)}),
               (SELECT e.id FROM epg_epgdata e
                  JOIN epg_epgsource src ON src.id = e.epg_source_id AND src.is_active
                 WHERE lower(e.tvg_id) = lower({lit(tvg_id)}) LIMIT 1),
               0, now(), now(), false, false, false, 0;

        INSERT INTO dispatcharr_channels_channelstream (channel_id, stream_id, "order")
        SELECT c.id, s.id, ROW_NUMBER() OVER (ORDER BY a.priority DESC, s.id) - 1
        FROM dispatcharr_channels_channel c
        JOIN dispatcharr_channels_stream s ON lower(s.tvg_id) = lower(c.tvg_id)
        JOIN m3u_m3uaccount a ON a.id = s.m3u_account_id AND a.is_active
        WHERE lower(c.tvg_id) = lower({lit(tvg_id)});

        -- Without a profile row the channel exists but never reaches the TV's M3U.
        INSERT INTO dispatcharr_channels_channelprofilemembership (channel_id, channel_profile_id, enabled)
        SELECT c.id, p.id, true
        FROM dispatcharr_channels_channel c CROSS JOIN dispatcharr_channels_channelprofile p
        WHERE lower(c.tvg_id) = lower({lit(tvg_id)});
    """)

    guide = psql(f"""
        SELECT count(*) FROM epg_programdata p
        JOIN dispatcharr_channels_channel c ON c.epg_data_id = p.epg_id
        WHERE lower(c.tvg_id) = lower({lit(tvg_id)}) AND p.end_time > now();""")
    print(f"added  {number}  {name}   [{group}]")
    print(f"  streams : {len(streams)} ({', '.join(sorted({s[2] for s in streams}))})")
    print(f"  guide   : {guide[0][0] if guide else 0} upcoming programmes")

    if not whitelisted(tvg_id):
        if do_whitelist and add_to_whitelist(tvg_id):
            print("  whitelist: added — it will survive maintenance runs")
        else:
            print(f"  WARNING: {tvg_id} isn't whitelisted, so the next maintenance run deletes it.")
            print(f"           Re-run with --whitelist, or add it to _approved_tvgids by hand.")


def cmd_drop(tvg_id: str) -> None:
    rows = psql(f"SELECT id, name FROM dispatcharr_channels_channel WHERE lower(tvg_id)=lower({lit(tvg_id)});")
    if not rows:
        print("not in the lineup")
        return
    ids = ",".join(r[0] for r in rows)
    for t in ("channelprofilemembership", "channeloverride", "channelstream"):
        psql(f"DELETE FROM dispatcharr_channels_{t} WHERE channel_id IN ({ids});")
    psql(f"DELETE FROM dispatcharr_channels_channel WHERE id IN ({ids});")
    print("removed", ", ".join(r[1] for r in rows))


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 1
    if a[0] == "find" and len(a) > 1:
        cmd_find(" ".join(a[1:]))
    elif a[0] == "add" and len(a) > 1:
        num = int(a[a.index("--num") + 1]) if "--num" in a else None
        grp = a[a.index("--group") + 1] if "--group" in a else None
        cmd_add(a[1], num, grp, "--whitelist" in a)
    elif a[0] == "drop" and len(a) > 1:
        cmd_drop(a[1])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
