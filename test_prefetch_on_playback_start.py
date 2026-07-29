"""
Regression test for prefetch-on-playback-start (issue #62).

Playback start must stage the next episode but must NOT run any
completion-triggered step (keep-window deletion, finale release, sequential
advance, series-ended unmonitor, unmonitor-current). The watched event must
still do all of it.

Runs standalone: python test_prefetch_on_playback_start.py
"""
import sys
import types
from unittest import mock


def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_media_processor():
    """Import media_processor with its external deps stubbed out."""
    _stub_module('dotenv', load_dotenv=lambda *a, **k: None)
    _stub_module('requests',
                 get=mock.MagicMock(), put=mock.MagicMock(),
                 post=mock.MagicMock(), delete=mock.MagicMock(),
                 Session=mock.MagicMock())
    _stub_module('pending_deletions', PendingDeletions=mock.MagicMock())
    _stub_module('servarr_utils')
    _stub_module('episeerr',
                 normalize_url=lambda u: (u or '').rstrip('/'),
                 load_config=lambda: {'rules': {}},
                 save_config=lambda c: None,
                 load_global_settings=lambda: {})
    _stub_module('episeerr_utils',
                 reconcile_series_drift=lambda sid, cfg, series_data=None: (None, False),
                 http=mock.MagicMock())
    _stub_module('logging_config', main_logger=mock.MagicMock())
    _stub_module('settings_db',
                 get_sonarr_config=lambda: {'url': 'http://sonarr:8989', 'api_key': 'x'},
                 get_service=lambda *a, **k: None)
    import media_processor
    return media_processor


mp = _load_media_processor()

RULE = {
    'get_type': 'episodes', 'get_count': 1,
    'keep_type': 'episodes', 'keep_count': 1,
    'action_option': 'search',
    'monitor_watched': False,          # would unmonitor the current episode
    'release_keep_on_finale': True,    # destructive finale path enabled
    'unmonitor_on_series_ended': True, # destructive series-ended path enabled
    'always_have': '',
}

EPISODES = [
    {'id': 1, 'seasonNumber': 3, 'episodeNumber': 2, 'hasFile': True,  'episodeFileId': 11},
    {'id': 2, 'seasonNumber': 3, 'episodeNumber': 3, 'hasFile': True,  'episodeFileId': 12},
    {'id': 3, 'seasonNumber': 3, 'episodeNumber': 4, 'hasFile': True,  'episodeFileId': 13},
    {'id': 4, 'seasonNumber': 3, 'episodeNumber': 5, 'hasFile': False, 'episodeFileId': None},
]


def run(prefetch_only):
    """Invoke the webhook processor for S3E4 and capture what it did."""
    calls = {}
    with mock.patch.object(mp, 'fetch_all_episodes', return_value=list(EPISODES)), \
         mock.patch.object(mp, 'update_activity_date') as activity, \
         mock.patch.object(mp, 'unmonitor_episodes') as unmonitor, \
         mock.patch.object(mp, 'monitor_or_search_episodes') as prefetch, \
         mock.patch.object(mp, 'fetch_next_episodes_dropdown', return_value=[4]), \
         mock.patch.object(mp, 'delete_episodes_immediately') as delete, \
         mock.patch.object(mp, '_advance_sequential_if_finale') as advance, \
         mock.patch.object(mp, '_unmonitor_if_series_ended') as ended, \
         mock.patch.object(mp, 'is_anchor_episode', return_value=False), \
         mock.patch.object(mp, 'load_config', return_value={'rules': {}}), \
         mock.patch.object(mp, '_find_rule_name_for_series', return_value='testrule'):
        mp.process_episodes_for_webhook(
            series_id=42, season_number=3, episode_number=4,
            rule=dict(RULE), series_title='Test Show',
            prefetch_only=prefetch_only,
        )
        calls['activity'] = activity.call_count
        calls['unmonitor_current'] = unmonitor.call_count
        calls['prefetch'] = prefetch.call_count
        calls['delete'] = delete.call_count
        calls['advance'] = advance.call_count
        calls['series_ended'] = ended.call_count
    return calls


def check_payload_parsing():
    """get_server_activity must surface the prefetch_only flag from the payload."""
    import json as _json

    base = {
        'server_title': 'Test Show', 'server_season_num': 3, 'server_ep_num': 4,
        'thetvdb_id': '448463', 'themoviedb_id': None,
    }

    def parse(payload):
        with mock.patch.object(mp.os.path, 'exists', return_value=True), \
             mock.patch.object(mp, 'open', mock.mock_open(read_data=_json.dumps(payload)), create=True):
            return mp.get_server_activity()

    # Flag present and true → prefetch_only True
    assert parse({**base, 'prefetch_only': True})[5] is True, "prefetch_only=True must parse"
    # Flag present and false → full processing
    assert parse({**base, 'prefetch_only': False})[5] is False, "prefetch_only=False must parse"
    # Flag absent (legacy /webhook route, older payloads) → full processing
    assert parse(base)[5] is False, "absent prefetch_only must default to full processing"
    print("payload parsing: prefetch_only true/false/absent OK")


def main():
    check_payload_parsing()

    start = run(prefetch_only=True)
    watched = run(prefetch_only=False)

    # Playback start: stage the next episode, touch nothing destructive.
    assert start['prefetch'] == 1, f"playback start must prefetch: {start}"
    assert start['delete'] == 0, f"playback start must NOT delete: {start}"
    assert start['unmonitor_current'] == 0, f"playback start must NOT unmonitor current ep: {start}"
    assert start['advance'] == 0, f"playback start must NOT advance sequential: {start}"
    assert start['series_ended'] == 0, f"playback start must NOT unmonitor ended series: {start}"
    # Activity date still updates, so an actively-watched series is not grace-cleaned.
    assert start['activity'] == 1, f"playback start must still update activity date: {start}"

    # Watched event: unchanged behavior, cleanup still runs.
    assert watched['prefetch'] == 1, f"watched must prefetch: {watched}"
    assert watched['delete'] == 1, f"watched must still delete keep-block: {watched}"
    assert watched['unmonitor_current'] == 1, f"watched must unmonitor current ep: {watched}"
    assert watched['series_ended'] == 1, f"watched must run series-ended check: {watched}"

    print("playback start :", start)
    print("watched        :", watched)
    print("OK - prefetch runs on start, cleanup deferred to watched")


if __name__ == '__main__':
    main()
