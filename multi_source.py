"""
Multi-source watch-event coordination.

Allows more than one media server (e.g. Plex for one household, Jellyfin for
another) to send watch events to Episeerr without fighting over a series'
rolling episode window. Two mechanisms, both gated behind the
`multi_source_enabled` global setting (default: off, stock behavior):

1. Event dedup — the same series/season/episode event arriving again within
   `multi_source_dedup_minutes` (any source) is ignored. This also covers the
   classic double-processing case of Plex native webhooks + Tautulli both
   reporting one playback.

2. Series source affinity — each series is pinned to the first source that
   reports a watch event for it (`multi_source_affinity`, default on). Events
   from other sources still refresh the series activity date (so Grace and
   Dormant timers stay accurate) but do not advance or shrink the episode
   window. Affinity can be reassigned or cleared via the
   /api/multi-source/affinity endpoint.

   To avoid a stale pin locking a series forever (viewer abandons a show,
   someone on the other server picks it up later), a pin expires after
   `multi_source_pin_ttl_days` without an allowed event from its source
   (default 30, 0 = never); the next source to report the series takes it
   over.

State lives in data/multi_source_state.json. Because integrations spawn
media_processor.py as short-lived subprocesses, state access is serialized
with an fcntl lock so concurrent events from different servers cannot race.
"""

import fcntl
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.getcwd(), 'data', 'multi_source_state.json')
LOCK_FILE = STATE_FILE + '.lock'
SETTINGS_FILE = os.path.join(os.getcwd(), 'config', 'global_settings.json')

DEFAULT_DEDUP_MINUTES = 360
DEFAULT_PIN_TTL_DAYS = 30


def _load_settings():
    try:
        with open(SETTINGS_FILE, 'r') as fh:
            return json.load(fh)
    except Exception:
        return {}


def is_enabled():
    return bool(_load_settings().get('multi_source_enabled', False))


def _affinity_enabled(settings):
    return bool(settings.get('multi_source_affinity', True))


def _dedup_seconds(settings):
    try:
        minutes = int(settings.get('multi_source_dedup_minutes', DEFAULT_DEDUP_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_DEDUP_MINUTES
    return max(0, minutes) * 60


def _pin_ttl_seconds(settings):
    """Seconds of source silence before a pin may be taken over; None = never."""
    try:
        days = int(settings.get('multi_source_pin_ttl_days', DEFAULT_PIN_TTL_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_PIN_TTL_DAYS
    return days * 86400 if days > 0 else None


class _locked_state:
    """Context manager: exclusive lock + load/save of the state file."""

    def __enter__(self):
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        self._lock_fh = open(LOCK_FILE, 'w')
        fcntl.flock(self._lock_fh, fcntl.LOCK_EX)
        try:
            with open(STATE_FILE, 'r') as fh:
                self.state = json.load(fh)
        except Exception:
            self.state = {}
        self.state.setdefault('series_affinity', {})
        self.state.setdefault('recent_events', {})
        return self

    def save(self):
        tmp_path = STATE_FILE + '.tmp'
        with open(tmp_path, 'w') as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp_path, STATE_FILE)

    def __exit__(self, *exc):
        fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
        self._lock_fh.close()
        return False


def allow_event(series_id, season, episode, source, series_title=None):
    """
    Decide whether a watch event may drive window processing.

    Returns (allowed: bool, reason: str). Always allows when the feature is
    disabled. Never raises — on unexpected errors it fails open so a state
    problem cannot stall normal episode handling.
    """
    settings = _load_settings()
    if not settings.get('multi_source_enabled', False):
        return True, 'multi-source mode disabled'

    source = (source or 'unknown').strip().lower()
    series_key = str(series_id)
    event_key = f"{series_key}:S{season}E{episode}"
    now = time.time()
    window = _dedup_seconds(settings)
    ttl = _pin_ttl_seconds(settings)

    try:
        with _locked_state() as ls:
            events = ls.state['recent_events']
            for key in [k for k, v in events.items()
                        if now - v.get('ts', 0) > window]:
                del events[key]

            prior = events.get(event_key)
            if prior is not None:
                ls.save()  # persist pruning
                return False, (
                    f"duplicate: already processed from '{prior.get('source')}' "
                    f"{int(now - prior.get('ts', now))}s ago"
                )

            if _affinity_enabled(settings):
                affinity = ls.state['series_affinity']
                pinned = affinity.get(series_key)
                if pinned is not None and pinned.get('source') != source:
                    last_seen = (pinned.get('last_event_ts')
                                 or pinned.get('pinned_at') or 0)
                    if ttl is not None and now - last_seen > ttl:
                        logger.info(
                            f"Multi-source: pin on series {series_key} "
                            f"('{pinned.get('title') or 'unknown'}') expired, "
                            f"'{pinned.get('source')}' silent for "
                            f"{int((now - last_seen) / 86400)}d; "
                            f"'{source}' takes over")
                        pinned = None
                    else:
                        ls.save()  # persist pruning
                        return False, (
                            f"series pinned to '{pinned.get('source')}', "
                            f"event came from '{source}'"
                        )
                if pinned is None:
                    logger.info(
                        f"Multi-source: pinned series {series_key} to source '{source}'")
                pin = dict(pinned or {})
                pin.update({'source': source, 'last_event_ts': now})
                pin.setdefault('pinned_at', now)
                if series_title:
                    pin['title'] = series_title
                affinity[series_key] = pin

            events[event_key] = {'source': source, 'ts': now}
            ls.save()
            return True, 'allowed'
    except Exception as exc:
        logger.error(f"Multi-source: state error, failing open: {exc}")
        return True, f'state error ({exc}), failing open'


def get_state():
    """Snapshot of affinity pins and recent events for the API/UI."""
    try:
        with _locked_state() as ls:
            return {
                'enabled': is_enabled(),
                'series_affinity': ls.state['series_affinity'],
                'recent_events': ls.state['recent_events'],
            }
    except Exception as exc:
        return {'enabled': is_enabled(), 'error': str(exc)}


def set_affinity(series_id, source, series_title=None):
    """Pin a series to a source, or clear the pin when source is falsy."""
    series_key = str(series_id)
    with _locked_state() as ls:
        if source:
            prior = ls.state['series_affinity'].get(series_key) or {}
            pin = {
                'source': str(source).strip().lower(),
                'pinned_at': time.time(),
                'manual': True,
            }
            if series_title or prior.get('title'):
                pin['title'] = series_title or prior.get('title')
            ls.state['series_affinity'][series_key] = pin
        else:
            ls.state['series_affinity'].pop(series_key, None)
        ls.save()
    return get_state()
