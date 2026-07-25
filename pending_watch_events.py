"""
Pending Watch Events - queue for watch activity reconcile.py finds on a
media server that's newer than what Episeerr's config has on record, but
never acts on automatically (see reconcile.py for why). Same
queue-for-human-approval shape as pending_deletions.py, for a different
action: replay a watch event instead of delete a file.

File format: {"items": [...], "last_checked": <unix ts or None>}
"""
import os
import json
import time
import logging
from threading import Lock

logger = logging.getLogger(__name__)

PENDING_FILE = os.path.join(os.getcwd(), 'data', 'pending_watch_events.json')
os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)

_lock = Lock()


def _load_raw():
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f:
                data = json.load(f)
            data.setdefault('items', [])
            data.setdefault('last_checked', None)
            return data
    except Exception as e:
        logger.error(f"Error loading pending watch events: {e}")
    return {'items': [], 'last_checked': None}


def _save_raw(data):
    try:
        with open(PENDING_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving pending watch events: {e}")


def load_pending():
    """List of pending watch-event items."""
    with _lock:
        return _load_raw()['items']


def get_last_checked():
    with _lock:
        return _load_raw()['last_checked']


def mark_checked(timestamp=None):
    """Record when reconcile.check_for_missed_watch_events() last ran, for
    UI display only - not used for gating what gets detected next time."""
    with _lock:
        data = _load_raw()
        data['last_checked'] = timestamp or int(time.time())
        _save_raw(data)


def add_or_update_pending(series_id, series_title, season, episode, source, user, watched_at):
    """Queue a detected watch event. If a pending item already exists for
    this series, bump it forward to the newer (season, episode) instead of
    creating a duplicate - covers the case where a series sits unprocessed
    across more than one startup and the viewer kept watching in between."""
    with _lock:
        data = _load_raw()
        sid = str(series_id)
        for item in data['items']:
            if str(item['series_id']) == sid:
                if (season, episode) > (item['season'], item['episode']):
                    item.update(season=season, episode=episode, source=source,
                                user=user, watched_at=watched_at)
                    _save_raw(data)
                return

        data['items'].append({
            'id': f"{sid}-{int(time.time() * 1000)}",
            'series_id': series_id,
            'series_title': series_title,
            'season': season,
            'episode': episode,
            'source': source,
            'user': user,
            'watched_at': watched_at,
            'detected_at': int(time.time()),
        })
        _save_raw(data)


def get_item(item_id):
    for item in load_pending():
        if item['id'] == item_id:
            return item
    return None


def clear_pending(item_id):
    """Remove one item without acting on it."""
    with _lock:
        data = _load_raw()
        before = len(data['items'])
        data['items'] = [i for i in data['items'] if i['id'] != item_id]
        _save_raw(data)
        return len(data['items']) < before


def clear_all_pending():
    with _lock:
        data = _load_raw()
        data['items'] = []
        _save_raw(data)


def get_pending_summary():
    items = load_pending()
    return {'count': len(items), 'items': items}
