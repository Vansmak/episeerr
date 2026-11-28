"""
Missed watch-event detection.

Webhooks are fire-and-forget: if Episeerr is down, restarting, or a webhook
silently fails to fire, that watch event is lost and the affected series'
rolling episode window just stalls until the next watch. This module runs
once at startup and checks Plex/Jellyfin/Emby/Tautulli's own watch history
for anything newer than what Episeerr's config has on record for that
series.

Deliberately does NOT auto-replay what it finds. Every source here has some
gap between its own definition of "watched" and Episeerr's configured
detection threshold (see CHANGELOG/memory for the Jellyfin/Emby "position
resets once Played" dead end and the open Plex question) — silently
replaying an inferred event risks doing the wrong thing with no one aware
it happened. Instead, anything newer becomes a pending item in
pending_watch_events.py for a human to Process (run it through the exact
same path a live webhook would have) or Clear (ignore it). One run per
startup, no periodic interval - de-duplication lives in
pending_watch_events.add_or_update_pending(), keyed per series, not in a
sweep watermark.

Gated behind reconcile_enabled (default off) and skipped entirely while
automation_held is set.
"""

import logging
from datetime import datetime, timezone

from episeerr_utils import http, media_server_auth_headers

logger = logging.getLogger(__name__)


def _sweep_plex(since_ts=0):
    """Episodes watched on Plex since since_ts -> [(ts, series, season, ep, user)]."""
    from integrations.plex import _get_plex_detection_cfg

    cfg = _get_plex_detection_cfg()
    if not cfg['url'] or not cfg['api_key']:
        return []

    headers = {'X-Plex-Token': cfg['api_key'], 'Accept': 'application/json'}

    # Only needed to honor allowed_users - history returns accountID, not a
    # name, and real-time webhook filtering is by name.
    accounts_by_id = {}
    if cfg['allowed_users']:
        try:
            resp = http.get(f"{cfg['url']}/accounts", headers=headers, timeout=15)
            if resp.ok:
                for acct in (resp.json().get('MediaContainer') or {}).get('Account') or []:
                    accounts_by_id[str(acct.get('id'))] = acct.get('name')
        except Exception as exc:
            logger.warning(f"[reconcile] Plex account lookup failed: {exc}")

    resp = http.get(
        f"{cfg['url']}/status/sessions/history/all",
        headers=headers,
        params={'sort': 'viewedAt:desc', 'X-Plex-Container-Size': 500},
        timeout=30,
    )
    resp.raise_for_status()
    metadata = (resp.json().get('MediaContainer') or {}).get('Metadata') or []

    events = []
    for item in metadata:
        if item.get('type') != 'episode':
            continue
        viewed = item.get('viewedAt')
        if not viewed or int(viewed) <= since_ts:
            continue
        series = item.get('grandparentTitle')
        season = item.get('parentIndex')
        episode = item.get('index')
        if not series or season is None or episode is None:
            continue
        user = accounts_by_id.get(str(item.get('accountID')), 'Unknown')
        if cfg['allowed_users'] and user not in cfg['allowed_users']:
            continue
        events.append((int(viewed), series, int(season), int(episode), user))
    return events


def _parse_iso(value):
    """Jellyfin/Emby dates: ISO-8601 with variable fractional digits and 'Z'."""
    if not value:
        return None
    try:
        base = value.split('.')[0].rstrip('Z')
        return datetime.strptime(base, '%Y-%m-%dT%H:%M:%S').replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _sweep_emby_api(service_name, since_ts=0):
    """Shared sweep for Jellyfin/Emby - both speak the same Emby-derived API."""
    from integrations import get_integration

    integration = get_integration(service_name)
    if integration is None:
        return []
    config = integration.get_config()
    if not config or not config.get('url') or not config.get('api_key'):
        return []
    user_id = integration._resolve_user_id(config)
    if not user_id:
        logger.warning(f"[reconcile] {service_name}: could not resolve user")
        return []

    resp = http.get(
        f"{config['url'].rstrip('/')}/Users/{user_id}/Items",
        headers=media_server_auth_headers(config['api_key']),
        params={
            'IncludeItemTypes': 'Episode', 'Recursive': 'true',
            'Filters': 'IsPlayed', 'SortBy': 'DatePlayed', 'SortOrder': 'Descending',
            'Limit': 500, 'Fields': 'SeriesName,ParentIndexNumber,IndexNumber,UserData',
        },
        timeout=30,
    )
    resp.raise_for_status()

    user_name = config.get('user_id') or 'Unknown'
    events = []
    for item in resp.json().get('Items') or []:
        played_at = _parse_iso((item.get('UserData') or {}).get('LastPlayedDate'))
        if played_at is None or played_at <= since_ts:
            continue
        series = item.get('SeriesName')
        season = item.get('ParentIndexNumber')
        episode = item.get('IndexNumber')
        if not series or season is None or episode is None:
            continue
        events.append((played_at, series, int(season), int(episode), user_name))
    return events


def _sweep_tautulli(since_ts=0):
    """Episodes watched per Tautulli's own history since since_ts."""
    from episeerr_utils import get_tautulli_settings

    tautulli_url, api_key = get_tautulli_settings()
    if not tautulli_url or not api_key:
        return []

    resp = http.get(
        f"{tautulli_url}/api/v2",
        params={'apikey': api_key, 'cmd': 'get_history', 'media_type': 'episode',
                'length': 500, 'start': 0},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get('response', {}).get('result') != 'success':
        return []

    events = []
    for entry in data.get('response', {}).get('data', {}).get('data', []) or []:
        ts = entry.get('date')
        if not ts or int(ts) <= since_ts:
            continue
        series = entry.get('grandparent_title')
        season = entry.get('parent_media_index')
        episode = entry.get('media_index')
        if not series or season is None or episode is None:
            continue
        user = entry.get('friendly_name') or entry.get('user') or 'Unknown'
        events.append((int(ts), series, int(season), int(episode), user))
    return events


_SWEEPERS = (
    ('plex', _sweep_plex),
    ('jellyfin', lambda: _sweep_emby_api('jellyfin')),
    ('emby', lambda: _sweep_emby_api('emby')),
    ('tautulli', _sweep_tautulli),
)


def replay_watch_event(source, series, season, episode, user):
    """Run one watch event through the source's normal processing path -
    the same thing a live webhook would have done. Used by the pending
    item's "Process" action, never called automatically."""
    if source == 'tautulli':
        # Tautulli has no process_episode() - its shared processor takes a
        # raw webhook-shaped payload instead. No notification_type means
        # this is treated as a completed watch, same as a real "Watched"
        # webhook.
        from integrations.tautulli import process_watch_event
        result = process_watch_event({
            'plex_title': series,
            'plex_season_num': season,
            'plex_ep_num': episode,
        })
        return result.get('status') == 'success'

    from integrations import get_integration
    integration = get_integration(source)
    if integration is None:
        logger.warning(f"[reconcile] No '{source}' integration loaded, skipping replay")
        return False
    return integration.process_episode({
        'series_name': series,
        'season_number': season,
        'episode_number': episode,
        'user_name': user,
        'progress_percent': 100.0,
    })


def _is_newer_than_recorded(series_id, season, episode, config):
    """True if (season, episode) is newer than what config has recorded for
    this series. Handles both grace_scope 'series' (flat last_season/
    last_episode) and 'season' (per-season last_episode) tracking. False if
    the series isn't tracked by any rule at all - out of scope here, that's
    the Sonarr-tag orphan-recovery job (episeerr_utils.reconcile_series_drift),
    not this one."""
    sid = str(series_id)
    for rule in config.get('rules', {}).values():
        series_dict = rule.get('series', {})
        if sid not in series_dict:
            continue
        data = series_dict[sid]
        if not isinstance(data, dict):
            return True  # no usable record at all
        if rule.get('grace_scope') == 'season':
            season_data = (data.get('seasons') or {}).get(str(season))
            last_episode = season_data.get('last_episode') if season_data else None
            return last_episode is None or episode > last_episode
        last_season = data.get('last_season')
        last_episode = data.get('last_episode')
        if last_season is None or last_episode is None:
            return True
        return (season, episode) > (last_season, last_episode)
    return False


def check_for_missed_watch_events():
    """One-shot check across all configured sources, called once at startup.
    Queues anything newer than Episeerr's own records as a pending item;
    never replays automatically. Returns a summary dict; never raises."""
    from media_processor import load_global_settings, get_series_id, load_config
    import pending_watch_events

    summary = {'ran': False, 'found': 0, 'errors': []}
    try:
        settings = load_global_settings()
        if not settings.get('reconcile_enabled', False):
            return summary
        if settings.get('automation_held', False):
            logger.info("[reconcile] Skipping check - automation is held")
            return summary

        config = load_config()
        found = 0
        for label, sweep_fn in _SWEEPERS:
            try:
                events = sweep_fn()
            except Exception as exc:
                logger.warning(f"[reconcile] {label} check failed: {exc}")
                summary['errors'].append(f"{label}: {exc}")
                continue

            for ts, series, season, episode, user in events:
                series_id = get_series_id(series)
                if not series_id:
                    continue
                if not _is_newer_than_recorded(series_id, season, episode, config):
                    continue
                pending_watch_events.add_or_update_pending(
                    series_id=series_id, series_title=series, season=season,
                    episode=episode, source=label, user=user, watched_at=ts,
                )
                found += 1

        if found:
            logger.info(f"[reconcile] Found {found} watch event(s) newer than Episeerr's records")
        pending_watch_events.mark_checked()
        summary.update(ran=True, found=found)
        return summary
    except Exception as exc:
        logger.error(f"[reconcile] Unexpected error: {exc}", exc_info=True)
        summary['errors'].append(str(exc))
        return summary


def check_delay_tagged_series():
    """One-shot startup check for series still carrying episeerr_delay.

    The only way a series still has that tag is a missed SeriesAdd webhook -
    the live path (webhooks.py) always strips it via
    episeerr_utils.apply_initial_rule_selection() once it finishes. Unlike
    check_for_missed_watch_events(), this isn't gated behind reconcile_enabled:
    there's no inference here, the tag itself is an unambiguous "this was
    never processed" signal, so it's safe to act on directly rather than
    queueing for review. Still skipped while automation_held is set, and
    cancels anything Sonarr already grabbed while waiting before applying
    the rule, in case the delay profile's own timer ran out first.
    """
    from media_processor import load_global_settings, load_config
    from episeerr_utils import (
        http, SONARR_URL, get_sonarr_headers, get_tag_mapping,
        get_or_create_rule_tag_id, resolve_rule_from_tags,
        cancel_queued_downloads_for_series, apply_initial_rule_selection,
    )

    summary = {'ran': False, 'processed': 0, 'errors': []}
    try:
        settings = load_global_settings()
        if settings.get('automation_held', False):
            logger.info("[reconcile] Skipping delay-tag check - automation is held")
            return summary

        delay_tag_id = get_or_create_rule_tag_id('delay')
        if not delay_tag_id:
            summary['ran'] = True
            return summary

        headers = get_sonarr_headers()
        resp = http.get(f"{SONARR_URL}/api/v3/series", headers=headers, timeout=30)
        resp.raise_for_status()
        all_series = resp.json()

        tag_mapping = get_tag_mapping()
        config = load_config()

        processed = 0
        for series in all_series:
            tags = series.get('tags') or []
            if delay_tag_id not in tags:
                continue

            series_id = series.get('id')
            series_title = series.get('title', 'Unknown')
            assigned_rule, is_select_request = resolve_rule_from_tags(tags, tag_mapping, config)

            if is_select_request:
                logger.info(f"[reconcile] '{series_title}' carries episeerr_select + episeerr_delay - leaving for the selection workflow")
                continue
            if not assigned_rule:
                logger.warning(f"[reconcile] '{series_title}' carries episeerr_delay with no resolvable rule tag - add a rule tag to process it")
                continue

            logger.info(
                f"[reconcile] '{series_title}': episeerr_delay present with no live webhook having "
                f"processed it - Episeerr must have been down. Processing rule '{assigned_rule}' now"
            )
            try:
                cancel_queued_downloads_for_series(series_id)
                if apply_initial_rule_selection(series_id, series_title, assigned_rule, config, starting_season=1):
                    processed += 1
                config = load_config()  # reload - apply_initial_rule_selection may have modified it
            except Exception as exc:
                logger.error(f"[reconcile] Error processing delay-tagged series '{series_title}': {exc}")
                summary['errors'].append(f"{series_title}: {exc}")

        if processed:
            logger.info(f"[reconcile] Processed {processed} delay-tagged series")
        summary.update(ran=True, processed=processed)
        return summary
    except Exception as exc:
        logger.error(f"[reconcile] Unexpected error in delay-tag check: {exc}", exc_info=True)
        summary['errors'].append(str(exc))
        return summary


# How long after Sonarr adds a series the auto-assign fallback still applies
# the rule's full initial selection (unmonitor + monitor/search the rule's
# first episodes). Older stragglers are only assigned to the rule, so a show
# someone has already been watching for days doesn't get its monitoring reset.
AUTO_ASSIGN_APPLY_WINDOW_HOURS = 24


def _parse_ts(value):
    """ISO-8601 (Sonarr 'added', our own isoformat()) -> aware datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        # Sonarr can emit 7 fractional digits, which older fromisoformat rejects
        try:
            head, _, tail = str(value).partition('.')
            dt = datetime.fromisoformat(head.replace('Z', '') + '+00:00')
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_unassigned_new_series():
    """Fallback for 'Auto-assign new series to default rule' (episeerr#110).

    Auto-assign normally happens in the live SeriesAdd webhook. If that
    webhook never reaches Episeerr (not configured, "On Series Add" not
    ticked, wrong URL, Episeerr down), the series silently stays Unassigned.
    This sweep catches those: any Sonarr series with no episeerr tag, not
    tracked under any rule, and added to Sonarr *after* auto-assign was turned
    on, gets assigned to the default rule. Series that predate the toggle are
    left alone - leaving a show unassigned is a legitimate choice.

    Runs at startup and every scheduler tick. Skipped while automation_held.
    """
    from media_processor import load_global_settings, save_global_settings, load_config
    from episeerr_utils import (
        SONARR_URL, get_sonarr_headers, resolve_rule_from_tags,
        cancel_queued_downloads_for_series, apply_initial_rule_selection,
        ensure_series_tracked_under_rule, unmonitor_series,
    )

    summary = {'ran': False, 'assigned': 0, 'errors': []}
    try:
        settings = load_global_settings()
        if not settings.get('auto_assign_new_series', False):
            return summary
        if settings.get('automation_held', False):
            logger.info("[reconcile] Skipping auto-assign sweep - automation is held")
            return summary

        enabled_at = _parse_ts(settings.get('auto_assign_enabled_at'))
        if enabled_at is None:
            # Installs that turned auto-assign on before this timestamp existed:
            # start the clock now so only series added from here on are swept.
            settings['auto_assign_enabled_at'] = datetime.now(timezone.utc).isoformat()
            save_global_settings(settings)
            logger.info("[reconcile] Auto-assign sweep: recorded enable time, will catch series added from now on")
            summary['ran'] = True
            return summary

        config = load_config()
        default_rule = config.get('default_rule', 'default')
        if default_rule not in config.get('rules', {}):
            logger.warning(f"[reconcile] Auto-assign sweep: default rule '{default_rule}' not found - skipping")
            return summary

        tracked = set()
        for rule in config.get('rules', {}).values():
            tracked.update(str(sid) for sid in (rule.get('series') or {}).keys())

        headers = get_sonarr_headers()
        resp = http.get(f"{SONARR_URL}/api/v3/series", headers=headers, timeout=30)
        resp.raise_for_status()

        # Fetch tags directly rather than via get_tag_mapping(), which
        # swallows failures into {} - indistinguishable here from "this
        # Sonarr genuinely has zero tags". On a cold cache (e.g. this sweep's
        # own startup run racing Sonarr still coming up) that would make
        # resolve_rule_from_tags() below misread every already-tagged series
        # (episeerr_select awaiting a manual rule choice, or a real rule tag
        # not yet reflected in config) as untagged and reassign it. Raising
        # here instead aborts this whole tick (caught below) so a transient
        # API hiccup can't misfire against already-claimed series.
        tags_resp = http.get(f"{SONARR_URL}/api/v3/tag", headers=headers, timeout=30)
        tags_resp.raise_for_status()
        tag_mapping = {t['id']: t['label'] for t in tags_resp.json()}
        now = datetime.now(timezone.utc)

        assigned = 0
        for series in resp.json():
            series_id = series.get('id')
            if series_id is None or str(series_id) in tracked:
                continue
            added = _parse_ts(series.get('added'))
            if added is None or added <= enabled_at:
                continue
            rule_name, is_select = resolve_rule_from_tags(series.get('tags') or [], tag_mapping, config)
            if rule_name or is_select:
                continue  # tagged - the webhook/delay paths own it

            title = series.get('title', 'Unknown')
            try:
                age_hours = (now - added).total_seconds() / 3600
                if age_hours <= AUTO_ASSIGN_APPLY_WINDOW_HOURS:
                    logger.info(
                        f"[reconcile] '{title}' was added {age_hours:.1f}h ago without a SeriesAdd webhook "
                        f"reaching Episeerr - auto-assigning to '{default_rule}' and applying its initial selection. "
                        f"Check the Sonarr webhook has 'On Series Add' enabled."
                    )
                    cancel_queued_downloads_for_series(series_id)
                    unmonitor_series(series_id, headers)
                    apply_initial_rule_selection(series_id, title, default_rule, config, starting_season=1)
                else:
                    logger.info(
                        f"[reconcile] '{title}' was added {age_hours:.1f}h ago and never assigned - "
                        f"assigning to '{default_rule}' (monitoring left as-is)"
                    )
                    ensure_series_tracked_under_rule(series_id, default_rule, config)
                assigned += 1
                tracked.add(str(series_id))
                config = load_config()
            except Exception as exc:
                logger.error(f"[reconcile] Auto-assign failed for '{title}': {exc}")
                summary['errors'].append(f"{title}: {exc}")

        if assigned:
            logger.info(f"[reconcile] Auto-assigned {assigned} series to '{default_rule}'")
        summary.update(ran=True, assigned=assigned)
        return summary
    except Exception as exc:
        logger.error(f"[reconcile] Unexpected error in auto-assign sweep: {exc}", exc_info=True)
        summary['errors'].append(str(exc))
        return summary
