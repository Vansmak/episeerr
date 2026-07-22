"""
Game-Day Events Integration for Episeerr
─────────────────────────────────────────
On days a watchlist team (or a tracked UFC card) plays, find the broadcast
stream in Dispatcharr and inject it as a channel in a dedicated "Events
Today" group + reserved channel-number range, so it shows up cleanly in the
guide instead of being buried in the provider's raw channel list. Channels
disappear again once the game is no longer in the active whitelist.

Data flow:
    ESPN unofficial scoreboard API  ──►  match watchlist team/league
                                    ──►  regex-search Dispatcharr streams
                                    ──►  create/update "Events Today" channel
                                    ──►  store matchup/time/state in this
                                         module's own DB row (`today` events)
                                    ──►  stage active-events whitelist into
                                         the dispatcharr container + run
                                         events_teardown.sql to sweep expired
                                         channels

Dispatcharr has no API to create EPGData/guide entries (only GET on
/api/epg/epgdata/), so there is no real program-guide entry behind these
channels — the channel `name` itself carries the matchup + start time, and
richer structured data (real start_time, channel number) is served from this
module's own `/today` endpoint for Xadarr's home-screen hero to consume.

Endpoints registered:
    GET  /api/integration/events/groups          ← list M3U account groups + enabled state
    POST /api/integration/events/groups/enable    ← flip enabled+auto_channel_sync=false on selected groups
    GET  /api/integration/events/config           ← current watchlist/range/group config
    POST /api/integration/events/config           ← update config
    GET  /api/integration/events/today            ← today's injected events (Xadarr hero reads this)
    POST /api/integration/events/tick             ← manually trigger one pipeline pass (testing)
    GET  /api/integration/events/status           ← debug: thread state, last tick info
"""

import os
import time
import json
import logging
import threading
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from flask import Blueprint, jsonify, request

from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

SERVICE_TYPE = "game_events"

DEFAULT_GROUP_NAME = "Events Today"
DEFAULT_NUMBER_RANGE = [80, 99]
DEFAULT_TICK_MINUTES = 30
DEFAULT_WATCHLIST = [
    # rsn_hint: substring (case-insensitive) matched against ESPN's reported
    # broadcast names first, ahead of national networks (TBS/ESPN/FS1/etc.) --
    # MLB blacks out national broadcasts in the two teams' home markets, so
    # the team's own regional feed is the one Joe can actually watch even
    # when a national channel also legitimately airs the game (see
    # AMBIGUOUS_BROADCAST_AFFILIATES comment below for the related history).
    {"team": "Dodgers", "league": "mlb", "rsn_hint": "sportsnet la"},
    {"team": "Rams", "league": "nfl"},
    {"team": None, "league": "ufc"},   # no team filter -- track every main card
]

# Titan = m3u_account_id 11, Direct = 13 (see scripts/dispatcharr/maintenance.sql
# Part 9 Step D) -- keep the same provider priority for failover ordering.
PROVIDER_PRIORITY = {11: 0, 13: 1}

# National cable sports networks and dedicated regional sports networks (RSNs)
# broadcast one identical feed to everyone tuned in, so a name-search hit
# against Joe's existing lineup reliably means "this exact game is already
# carried" -- safe to skip creating a duplicate event channel.
#
# This used to be a fixed allowlist of national-only network names, checked
# against just ESPN's *first* reported broadcast name. Both were wrong for a
# Dodgers game broadcast regionally: ESPN reports broadcasts as
# ["MLB.TV", "TBS", "Sportsnet LA", "NBC Sports Phil"] with the streaming-only
# "MLB.TV" placeholder first, so the old check on networks[0] alone never even
# reached "TBS" (which WAS allowlisted) or "Sportsnet LA" (an RSN, not
# allowlisted at all) -- created a redundant channel instead of recognizing
# the RSN already in Joe's lineup. Now every reported network name is tried,
# and any dedicated sports/cable network name is trusted.
#
# Local broadcast affiliates (NBC/CBS/ABC/FOX/CW/PBS) are the one category
# still excluded, by name here rather than allowlisted above: they run
# different regional programming most of the time and only sometimes happen
# to be showing a given game's national feed. A name-search hit on "NBC" just
# means Joe's local NBC affiliate channel exists -- not that it's airing
# *this* game right now. Dispatcharr has no real EPG behind these channels to
# verify that (same root limitation noted in the file header). Treating a
# broadcast affiliate as "already carried" produced a real false negative: an
# MLB doubleheader nightcap on NBC/Peacock never got a channel created for it,
# silently, every tick, with no indication to Joe that it had been
# suppressed. RSNs and national cable/sports networks don't have this
# ambiguity -- they're single-purpose sports channels, not general
# broadcasters, so a name match is a reliable signal on its own.
AMBIGUOUS_BROADCAST_AFFILIATES = {
    "abc", "cbs", "nbc", "fox", "cw", "the cw", "pbs", "telemundo", "univision", "mynetworktv",
}

ESPN_LEAGUE_PATH = {
    "mlb": ("baseball", "mlb"),
    "nfl": ("football", "nfl"),
    "ufc": ("mma", "ufc"),
}

# scripts/dispatcharr/ is bind-mounted into the episeerr container at this
# same absolute host path (see docker inspect episeerr .Mounts) -- matches
# the hardcoded path dispatcharr.py already uses for maintenance.sql.
TEARDOWN_SQL_PATH = "/home/joe/projects/episeerr_custom/scripts/dispatcharr/events_teardown.sql"
CONTAINER_WHITELIST_PATH = "/events_whitelist.txt"
LOCAL_STAGING_PATH = "/tmp/events_whitelist_stage.txt"

_thread: Optional[threading.Thread] = None
_running = False
_last_tick: Optional[datetime] = None
_last_tick_summary: Dict[str, Any] = {}


# ══════════════════════════════════════════════════════════════════
#  Config + event-store persistence (services table, no new table)
# ══════════════════════════════════════════════════════════════════

def _get_config() -> Dict[str, Any]:
    from settings_db import get_service, save_service
    svc = get_service(SERVICE_TYPE, "config")
    if svc and svc.get("config"):
        return svc["config"]
    default_cfg = {
        "watchlist": DEFAULT_WATCHLIST,
        "channel_number_range": DEFAULT_NUMBER_RANGE,
        "group_name": DEFAULT_GROUP_NAME,
        "tick_minutes": DEFAULT_TICK_MINUTES,
        "enabled": True,
    }
    save_service(SERVICE_TYPE, "config", url="internal", api_key=None, config=default_cfg, enabled=True)
    return default_cfg


def _save_config(cfg: Dict[str, Any]) -> None:
    from settings_db import save_service
    save_service(SERVICE_TYPE, "config", url="internal", api_key=None, config=cfg, enabled=True)


def _get_today_events() -> List[Dict]:
    from settings_db import get_service
    svc = get_service(SERVICE_TYPE, "today")
    if svc and svc.get("config"):
        return svc["config"].get("events", [])
    return []


def _save_today_events(events: List[Dict]) -> None:
    from settings_db import save_service
    save_service(
        SERVICE_TYPE, "today", url="internal", api_key=None,
        config={"events": events, "updated_at": datetime.now(timezone.utc).isoformat()},
        enabled=True,
    )


# ══════════════════════════════════════════════════════════════════
#  Dispatcharr API client (reuses the same url/api_key already saved
#  for the `dispatcharr` integration -- no separate credentials)
# ══════════════════════════════════════════════════════════════════

def _dispatcharr_config() -> Optional[Dict[str, str]]:
    from settings_db import get_service
    svc = get_service("dispatcharr", "default")
    if not svc or not svc.get("url"):
        return None
    return {"url": svc["url"].rstrip("/"), "api_key": svc.get("api_key") or ""}


def _dispatcharr_request(method: str, path: str, **kwargs) -> Optional[requests.Response]:
    cfg = _dispatcharr_config()
    if not cfg:
        logger.warning("[Events] Dispatcharr not configured")
        return None
    headers = kwargs.pop("headers", {})
    headers["X-API-Key"] = cfg["api_key"]
    try:
        return requests.request(method, f"{cfg['url']}{path}", headers=headers, timeout=15, **kwargs)
    except Exception as exc:
        logger.warning(f"[Events] Dispatcharr request failed {method} {path}: {exc}")
        return None


def _paged_results(resp: Optional[requests.Response]) -> List[Dict]:
    if not resp or not resp.ok:
        return []
    data = resp.json()
    return data.get("results", data) if isinstance(data, dict) else data


# ══════════════════════════════════════════════════════════════════
#  One-time group-enable step (manual, confirmable -- never silent)
# ══════════════════════════════════════════════════════════════════

def list_m3u_groups() -> List[Dict]:
    """Joined view of every M3U account's groups + current enabled state,
    for Joe to review before flipping anything."""
    accounts = _paged_results(_dispatcharr_request("GET", "/api/m3u/accounts/"))
    group_names = {
        g["id"]: g["name"]
        for g in _paged_results(_dispatcharr_request("GET", "/api/channels/groups/"))
    }
    out = []
    for acct in accounts:
        for cg in acct.get("channel_groups", []):
            gid = cg.get("channel_group")
            out.append({
                "account_id": acct["id"],
                "account_name": acct.get("name"),
                "group_id": gid,
                "group_name": group_names.get(gid, "?"),
                "enabled": cg.get("enabled"),
                "auto_channel_sync": cg.get("auto_channel_sync"),
                "stream_count": cg.get("stream_count"),
            })
    return out


def enable_groups(selections: List[Dict[str, int]]) -> Dict[str, Any]:
    """selections: [{"account_id": 11, "group_id": 5}, ...].

    Sets enabled=true (streams sync into the DB, searchable) and
    auto_channel_sync=false (Dispatcharr does NOT auto-create a channel per
    stream -- this pipeline creates only the one matched channel per game).
    """
    by_account: Dict[int, List[int]] = {}
    for sel in selections:
        by_account.setdefault(sel["account_id"], []).append(sel["group_id"])

    results = {}
    for account_id, group_ids in by_account.items():
        body = {"channel_groups": [
            {"channel_group": gid, "enabled": True, "auto_channel_sync": False}
            for gid in group_ids
        ]}
        patch_resp = _dispatcharr_request("PATCH", f"/api/m3u/accounts/{account_id}/group-settings/", json=body)
        refresh_resp = _dispatcharr_request("POST", f"/api/m3u/refresh/{account_id}/")
        results[str(account_id)] = {
            "patch_status": patch_resp.status_code if patch_resp else None,
            "refresh_status": refresh_resp.status_code if refresh_resp else None,
        }
    return results


# ══════════════════════════════════════════════════════════════════
#  ESPN schedule lookup (unofficial, no key required)
# ══════════════════════════════════════════════════════════════════

def fetch_espn_scoreboard(league: str, date: Optional[str] = None) -> List[Dict]:
    sport, lg = ESPN_LEAGUE_PATH.get(league.lower(), (None, None))
    if not sport:
        logger.warning(f"[Events] Unknown league {league!r}")
        return []
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard"
    params = {"dates": date} if date else {}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception as exc:
        logger.warning(f"[Events] ESPN scoreboard fetch failed for {league}: {exc}")
        return []


def _parse_espn_event(ev: dict, league: str) -> Optional[Dict]:
    try:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        teams = [c.get("team", {}).get("displayName") or c.get("team", {}).get("name") for c in competitors]
        teams = [t for t in teams if t]
        networks: List[str] = []
        for b in comp.get("broadcasts", []):
            networks.extend(b.get("names", []))
        state = ((ev.get("status") or {}).get("type") or {}).get("state", "pre")
        return {
            "id": str(ev.get("id")),
            "league": league,
            "name": ev.get("name") or comp.get("shortName") or " @ ".join(teams) or "Event",
            "teams": teams,
            "start_time": ev.get("date"),
            "state": state,  # pre|in|post -- advisory only; Xadarr recomputes from start_time
            "networks": networks,
        }
    except Exception as exc:
        logger.debug(f"[Events] Could not parse ESPN event: {exc}")
        return None


def _team_matches(team: Optional[str], game: Dict) -> bool:
    if not team:
        return True  # no team filter configured for this watchlist entry (e.g. UFC)
    needle = team.lower()
    return any(needle in (t or "").lower() for t in game["teams"])


# ══════════════════════════════════════════════════════════════════
#  Stream matching + channel injection
# ══════════════════════════════════════════════════════════════════

def _search_streams(term: str) -> List[Dict]:
    return _paged_results(_dispatcharr_request("GET", "/api/channels/streams/", params={"search": term}))


def _find_candidate_streams(game: Dict) -> List[Dict]:
    team_terms = [t.split()[-1] for t in game["teams"] if t]
    terms = list(team_terms) + list(game.get("networks", []))
    hits, seen_ids = [], set()
    for term in terms:
        for s in _search_streams(term):
            if s["id"] not in seen_ids:
                seen_ids.add(s["id"])
                hits.append(s)
    return hits


def _select_streams(hits: List[Dict], game: Dict) -> List[Dict]:
    """Pick the stream(s) that plausibly cover this matchup -- prefer one
    where both team names appear in the stream name, one per provider,
    Titan-then-Direct ordering to match existing failover priority."""
    team_terms = [t.split()[-1].lower() for t in game["teams"] if t]

    def score(s: Dict) -> int:
        name = (s.get("name") or "").lower()
        return sum(1 for t in team_terms if t in name)

    candidates = [s for s in hits if score(s) > 0] if team_terms else hits
    if team_terms:
        both = [s for s in candidates if score(s) >= 2]
        candidates = both or candidates

    by_account: Dict[Optional[int], Dict] = {}
    for s in candidates:
        acct = s.get("m3u_account")
        by_account.setdefault(acct, s)
    ordered = sorted(by_account.keys(), key=lambda a: PROVIDER_PRIORITY.get(a, 99))
    return [by_account[a] for a in ordered]


def _find_carrying_channel(network: str) -> Optional[Dict]:
    """Return the first existing channel whose name matches this broadcast
    network that ISN'T one this pipeline created itself, if any. Used both
    to decide "already carried" and to surface that channel's number/name
    to Xadarr without creating a duplicate. Read-only -- this pipeline never
    writes to a channel it didn't create itself.

    Filters by tvg_id prefix ("events:<league>:<id>", set at creation time),
    not channel_group_id: group_name is configured as the real "Sports"
    group (not a separate events-only group), so a genuine pre-existing
    channel like ESPN lives in the *same* group as pipeline-created ones --
    group membership alone can't tell them apart. tvg_id can, and it's the
    same marker events_teardown.sql already keys its sweep on.
    """
    if not network:
        return None
    items = _paged_results(_dispatcharr_request("GET", "/api/channels/channels/", params={"search": network}))
    for c in items:
        if not (c.get("tvg_id") or "").startswith("events:"):
            return c
    return None


def _format_matchup_name(game: Dict) -> str:
    time_str = ""
    if game.get("start_time"):
        try:
            start = datetime.fromisoformat(game["start_time"].replace("Z", "+00:00"))
            local = start.astimezone()
            time_str = local.strftime("%I:%M%p").lstrip("0").lower()
        except Exception:
            time_str = ""
    label = game.get("name") or " @ ".join(game.get("teams", []))
    return f"{label} {time_str}".strip()


_events_group_cache: Dict[str, Any] = {}


def _get_or_create_group(name: str) -> Optional[int]:
    if _events_group_cache.get("name") == name and _events_group_cache.get("id"):
        return _events_group_cache["id"]
    # `search` is silently ignored on this endpoint (confirmed against the live
    # instance) -- the full list is small (~650 groups) so fetch-all + client-side
    # exact match is cheap and correct regardless.
    for g in _paged_results(_dispatcharr_request("GET", "/api/channels/groups/")):
        if g.get("name") == name:
            _events_group_cache.update({"name": name, "id": g["id"]})
            return g["id"]
    resp = _dispatcharr_request("POST", "/api/channels/groups/", json={"name": name})
    if resp and resp.ok:
        gid = resp.json()["id"]
        _events_group_cache.update({"name": name, "id": gid})
        return gid
    logger.error(f"[Events] Could not get/create group {name!r}")
    return None


def _next_channel_number(start: int, end: int) -> Optional[float]:
    resp = _dispatcharr_request("GET", "/api/channels/channels/numbers-in-range/", params={"start": start, "end": end})
    occupied = set()
    if resp and resp.ok:
        for occ in resp.json().get("occupants", []):
            n = occ.get("channel_number") or occ.get("effective_channel_number")
            if n is not None:
                try:
                    occupied.add(int(float(n)))
                except (TypeError, ValueError):
                    pass
    for n in range(start, end + 1):
        if n not in occupied:
            return float(n)
    logger.error(f"[Events] No free channel numbers in range {start}-{end}")
    return None


def _get_registry() -> Dict[str, int]:
    """channel_key -> Dispatcharr channel id, persisted so we always know
    which channel we already created for a game -- looking this up via a
    Dispatcharr search/filter is not reliable (see history below)."""
    from settings_db import get_service
    svc = get_service(SERVICE_TYPE, "registry")
    if svc and svc.get("config"):
        return dict(svc["config"].get("channel_ids", {}))
    return {}


def _save_registry(registry: Dict[str, int]) -> None:
    from settings_db import save_service
    save_service(SERVICE_TYPE, "registry", url="internal", api_key=None,
                  config={"channel_ids": registry}, enabled=True)


def _find_existing_event_channel(channel_key: str, registry: Dict[str, int]) -> Optional[Dict]:
    """Look up by the channel id recorded in our own registry, not by
    searching Dispatcharr. Two things ruled that out: `search` silently
    ignores tvg_id (only matches `name`), and an earlier version scoped the
    lookup to the *current* target group -- which broke the moment
    `group_name` was reconfigured, since the previously-created channel was
    still sitting in the old group and became invisible to the new search,
    producing a duplicate. Falls back to None (treated as "create fresh") if
    the recorded id no longer exists (deleted by teardown or by hand)."""
    channel_id = registry.get(channel_key)
    if not channel_id:
        return None
    resp = _dispatcharr_request("GET", f"/api/channels/channels/{channel_id}/")
    return resp.json() if resp and resp.ok else None


def _ensure_channel_for_game(cfg: Dict, game: Dict, registry: Dict[str, int]) -> Optional[Dict]:
    """Always returns a result for a matched watchlist game (so Xadarr's
    /today can show matchup/time even when there's nothing to play) --
    result["owned"] tells run_tick whether this pipeline is responsible for
    the channel (only "owned" channels go into the registry / teardown
    whitelist; an existing real channel found via _find_carrying_channel or
    a not-yet-resolved game is reported read-only and never touched)."""
    matchup_name = _format_matchup_name(game)
    channel_key = f"events:{game['league']}:{game['id']}"

    def _info_result(channel: Optional[Dict] = None) -> Dict:
        return {
            "key": channel_key,
            "matchup": matchup_name,
            "league": game["league"],
            "teams": game["teams"],
            "channel_id": channel["id"] if channel else None,
            "channel_number": (channel.get("channel_number") or channel.get("effective_channel_number")) if channel else None,
            "channel_name": channel.get("name") if channel else None,
            "start_time": game["start_time"],
            "state": game["state"],
            "owned": False,
        }

    # Try the watchlist entry's own RSN hint before ESPN's reported order --
    # national broadcasts (TBS/ESPN/FOX/etc.) are usually blacked out in the
    # two teams' home markets, so if Joe's own team's RSN is in the list at
    # all, it's the feed he can actually watch regardless of where ESPN
    # happens to list it relative to the national feed.
    rsn_hint = (game.get("rsn_hint") or "").lower()
    reported_networks = [n for n in (game.get("networks") or []) if n]
    ordered_networks = reported_networks
    if rsn_hint:
        hinted = [n for n in reported_networks if rsn_hint in n.lower()]
        rest = [n for n in reported_networks if rsn_hint not in n.lower()]
        ordered_networks = hinted + rest

    for network_name in ordered_networks:
        if network_name.lower() in AMBIGUOUS_BROADCAST_AFFILIATES:
            continue
        carrying = _find_carrying_channel(network_name)
        if carrying:
            logger.info(
                f"[Events] {game['name']!r} airs on {network_name!r}, already carried on "
                f"channel {carrying.get('channel_number')} -- not creating a duplicate"
            )
            return _info_result(carrying)

    hits = _find_candidate_streams(game)
    picked = _select_streams(hits, game)
    if not picked:
        logger.info(f"[Events] No stream match yet for {game['name']!r} -- listing as info-only this tick")
        return _info_result()

    group_name = cfg.get("group_name", DEFAULT_GROUP_NAME)
    group_id = _get_or_create_group(group_name)
    if group_id is None:
        return _info_result()

    stream_ids = [s["id"] for s in picked]

    existing = _find_existing_event_channel(channel_key, registry)
    if existing:
        channel_id = existing["id"]
        channel_number = existing.get("channel_number") or existing.get("effective_channel_number")
        _dispatcharr_request(
            "PATCH", f"/api/channels/channels/{channel_id}/",
            json={"name": matchup_name, "channel_group_id": group_id, "streams": stream_ids},
        )
    else:
        lo, hi = cfg.get("channel_number_range", DEFAULT_NUMBER_RANGE)
        channel_number = _next_channel_number(int(lo), int(hi))
        if channel_number is None:
            return None
        resp = _dispatcharr_request("POST", "/api/channels/channels/from-stream/", json={
            "stream_id": stream_ids[0],
            "channel_number": channel_number,
            "name": matchup_name,
        })
        if not resp or not resp.ok:
            logger.warning(f"[Events] from-stream failed for {game['name']!r}: {resp.status_code if resp else 'no response'}")
            return None
        channel_id = resp.json()["id"]
        _dispatcharr_request(
            "PATCH", f"/api/channels/channels/{channel_id}/",
            json={"channel_group_id": group_id, "streams": stream_ids, "tvg_id": channel_key},
        )
        logger.info(f"[Events] Created channel {channel_number} for {matchup_name!r}")

    return {
        "key": channel_key,
        "matchup": matchup_name,
        "league": game["league"],
        "teams": game["teams"],
        "channel_id": channel_id,
        "channel_number": channel_number,
        "channel_name": matchup_name,
        "start_time": game["start_time"],
        "state": game["state"],
        "owned": True,
    }


# ══════════════════════════════════════════════════════════════════
#  Teardown: stage whitelist into the dispatcharr container via
#  `docker cp` (either fully lands or fully fails -- never a partial
#  file), then run events_teardown.sql standalone. If staging fails,
#  teardown is skipped entirely for this tick rather than risk running
#  the sweep against a missing/stale whitelist.
# ══════════════════════════════════════════════════════════════════

def _run_teardown(active_keys: Set[str]) -> None:
    try:
        with open(LOCAL_STAGING_PATH, "w") as f:
            for key in sorted(active_keys):
                f.write(key + "\n")
        subprocess.run(
            ["docker", "cp", LOCAL_STAGING_PATH, f"dispatcharr:{CONTAINER_WHITELIST_PATH}"],
            check=True, capture_output=True, timeout=15,
        )
    except Exception as exc:
        logger.error(f"[Events] Could not stage whitelist into dispatcharr, skipping teardown this tick: {exc}")
        return

    try:
        result = subprocess.run(
            ["docker", "exec", "-i", "dispatcharr", "psql", "-U", "postgres", "-d", "dispatcharr"],
            input=open(TEARDOWN_SQL_PATH).read(),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            logger.info("[Events] Teardown sweep completed")
        else:
            logger.error(f"[Events] Teardown sweep failed: {result.stderr}")
    except Exception as exc:
        logger.error(f"[Events] Teardown sweep error: {exc}")


# ══════════════════════════════════════════════════════════════════
#  Pipeline tick
# ══════════════════════════════════════════════════════════════════

def run_tick() -> Dict[str, Any]:
    global _last_tick, _last_tick_summary
    cfg = _get_config()

    if not _dispatcharr_config():
        summary = {"status": "skipped", "reason": "dispatcharr not configured"}
        _last_tick, _last_tick_summary = datetime.now(timezone.utc), summary
        return summary

    today = datetime.now().strftime("%Y%m%d")
    matched_games: List[Dict] = []
    for entry in cfg.get("watchlist", []):
        league = (entry.get("league") or "").lower()
        team = entry.get("team")
        if not league:
            continue
        for ev in fetch_espn_scoreboard(league, date=today):
            game = _parse_espn_event(ev, league)
            if game and _team_matches(team, game):
                game["rsn_hint"] = entry.get("rsn_hint")
                matched_games.append(game)

    registry = _get_registry()
    active_keys: Set[str] = set()
    events_out: List[Dict] = []
    for game in matched_games:
        result = _ensure_channel_for_game(cfg, game, registry)
        if not result:
            continue
        owned = result.pop("owned", False)
        events_out.append(result)
        # Only pipeline-created channels are tracked in the registry / swept
        # by teardown -- an already-carried or not-yet-resolved game is
        # reported to Xadarr but this pipeline never owns or touches that
        # channel, so it must never end up in the whitelist/registry logic.
        if owned:
            active_keys.add(result["key"])
            registry[result["key"]] = result["channel_id"]

    # Prune to only currently-active *owned* keys -- self-healing, no
    # unbounded growth, and no stale entries pointing at channels teardown
    # already deleted.
    _save_registry({k: v for k, v in registry.items() if k in active_keys})

    _save_today_events(events_out)
    _run_teardown(active_keys)

    summary = {
        "status": "ok",
        "games_today": len(matched_games),
        "channels_active": len(events_out),
    }
    _last_tick, _last_tick_summary = datetime.now(timezone.utc), summary
    logger.info(f"[Events] Tick complete: {summary}")
    return summary


# ══════════════════════════════════════════════════════════════════
#  Background scheduler (daemon thread + sleep loop, matching the
#  existing trakt.py / plex.py convention -- no APScheduler dependency)
# ══════════════════════════════════════════════════════════════════

def start_scheduler() -> None:
    global _thread, _running
    if _running:
        return

    def _loop():
        global _running
        _running = True
        time.sleep(30)
        while _running:
            try:
                run_tick()
            except Exception as exc:
                logger.error(f"[Events] Scheduled tick error: {exc}", exc_info=True)
            try:
                interval = _get_config().get("tick_minutes", DEFAULT_TICK_MINUTES)
            except Exception:
                interval = DEFAULT_TICK_MINUTES
            time.sleep(max(int(interval), 5) * 60)

    _thread = threading.Thread(target=_loop, daemon=True, name="game-events-tick")
    _thread.start()
    logger.info("[Events] Game-day scheduler started")


def stop_scheduler() -> None:
    global _running
    _running = False
    logger.info("[Events] Game-day scheduler stopped")


# ══════════════════════════════════════════════════════════════════
#  Integration class
# ══════════════════════════════════════════════════════════════════

class EventsIntegration(ServiceIntegration):

    @property
    def service_name(self) -> str:
        return "events"

    @property
    def display_name(self) -> str:
        return "Game-Day Events"

    @property
    def description(self) -> str:
        return "Auto-injects watchlist team/UFC broadcasts into Dispatcharr as an Events Today channel"

    @property
    def icon(self) -> str:
        return "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/dispatcharr.png"

    @property
    def category(self) -> str:
        return "utility"

    @property
    def default_port(self) -> int:
        return 5002

    def test_connection(self, url: str, api_key: str, **kwargs) -> Tuple[bool, str]:
        return (True, "Uses the existing Dispatcharr connection") if _dispatcharr_config() else \
               (False, "Dispatcharr is not configured yet")

    def get_dashboard_stats(self, url: str = None, api_key: str = None) -> Dict[str, Any]:
        return {"configured": True, "events_today": len(_get_today_events())}

    def get_dashboard_widget(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "pill": {
                "icon": "fas fa-tv",
                "icon_color": "text-warning",
                "template": "{events_today} events today",
                "fields": ["events_today"],
            },
        }

    def create_blueprint(self) -> Blueprint:
        bp = Blueprint("events_integration", __name__, url_prefix="/api/integration/events")

        @bp.route("/groups", methods=["GET"])
        def groups():
            return jsonify({"groups": list_m3u_groups()})

        @bp.route("/groups/enable", methods=["POST"])
        def groups_enable():
            body = request.get_json(silent=True) or {}
            selections = body.get("selections", [])
            if not selections:
                return jsonify({"status": "error", "message": "No selections provided"}), 400
            return jsonify({"status": "ok", "results": enable_groups(selections)})

        @bp.route("/config", methods=["GET"])
        def config_get():
            return jsonify(_get_config())

        @bp.route("/config", methods=["POST"])
        def config_set():
            body = request.get_json(silent=True) or {}
            cfg = _get_config()
            cfg.update(body)
            _save_config(cfg)
            return jsonify(cfg)

        @bp.route("/today", methods=["GET"])
        def today():
            return jsonify({"events": _get_today_events()})

        @bp.route("/tick", methods=["POST"])
        def tick():
            return jsonify(run_tick())

        @bp.route("/status", methods=["GET"])
        def status():
            return jsonify({
                "scheduler_running": _running,
                "last_tick": _last_tick.isoformat() if _last_tick else None,
                "last_tick_summary": _last_tick_summary,
            })

        return bp


# Auto-discovery: episeerr scans for this module-level variable
integration = EventsIntegration()

if _get_config().get("enabled", True):
    start_scheduler()
