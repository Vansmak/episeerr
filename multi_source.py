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


def allow_event(series_id, season, episode, source):
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

    try:
        with _locked_state() as ls:
            events = ls.state['recent_events']
            for key in [k for k, v in events.items()
                        if now - v.get('ts', 0) > window]:
                del events[key]

            prior = events.get(event_key)
            if prior is not None:
                return False, (
                    f"duplicate: already processed from '{prior.get('source')}' "
                    f"{int(now - prior.get('ts', now))}s ago"
                )

            if _affinity_enabled(settings):
                affinity = ls.state['series_affinity']
                pinned = affinity.get(series_key)
                if pinned is None:
                    affinity[series_key] = {'source': source, 'pinned_at': now}
                    logger.info(
                        f"Multi-source: pinned series {series_key} to source '{source}'")
                elif pinned.get('source') != source:
                    ls.save()  # persist any pruning
                    return False, (
                        f"series pinned to '{pinned.get('source')}', "
                        f"event came from '{source}'"
                    )

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


def set_affinity(series_id, source):
    """Pin a series to a source, or clear the pin when source is falsy."""
    series_key = str(series_id)
    with _locked_state() as ls:
        if source:
            ls.state['series_affinity'][series_key] = {
                'source': str(source).strip().lower(),
                'pinned_at': time.time(),
                'manual': True,
            }
        else:
            ls.state['series_affinity'].pop(series_key, None)
        ls.save()
    return get_state()
