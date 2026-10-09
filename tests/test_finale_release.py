"""
Tests for release_keep_on_finale's next-episode gate (issue #97).

The finale keep window should only stay protected when the next season's first
episode is actually ready (has a file, or is grabbed in Sonarr's queue) - not
merely because a later season is announced in Sonarr.

Self-contained stdlib unittest, run with:

    python3 -m unittest tests.test_finale_release -v

media_processor's external deps (Flask app, settings DB, HTTP) are stubbed only
for the duration of its import, via patch.dict on sys.modules, so other test
modules in the same run still see the real modules.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix='episeerr_finale_test_')
for _var, _name in (('LOG_PATH', 'app.log'), ('MISSING_LOG_PATH', 'missing.log'),
                    ('CLEANUP_LOG_PATH', 'cleanup.log')):
    os.environ.setdefault(_var, os.path.join(_TMP, _name))


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _load_media_processor():
    stubs = {
        'dotenv': _stub('dotenv', load_dotenv=lambda *a, **k: None),
        'pending_deletions': _stub('pending_deletions', PendingDeletions=mock.MagicMock()),
        'episeerr': _stub('episeerr', normalize_url=lambda u: (u or '').rstrip('/')),
        'episeerr_utils': _stub('episeerr_utils',
                                reconcile_series_drift=lambda *a, **k: (None, False),
                                http=mock.MagicMock()),
        'logging_config': _stub('logging_config', main_logger=mock.MagicMock()),
        'settings_db': _stub('settings_db',
                             get_sonarr_config=lambda: {'url': 'http://sonarr:8989', 'api_key': 'x'},
                             get_service=lambda *a, **k: None),
    }
    saved = sys.modules.pop('media_processor', None)
    with mock.patch.dict(sys.modules, stubs):
        import media_processor
        mod = media_processor
    # Don't leave the stub-wired module behind for other tests.
    sys.modules.pop('media_processor', None)
    if saved is not None:
        sys.modules['media_processor'] = saved
    return mod


mp = _load_media_processor()

RULE = {
    'get_type': 'episodes', 'get_count': 1,
    'keep_type': 'episodes', 'keep_count': 3,
    'action_option': 'search',
    'monitor_watched': True,
    'release_keep_on_finale': True,
    'always_have': '',
}


def _ep(eid, season, episode, has_file, air='2025-01-01T00:00:00Z'):
    return {'id': eid, 'seasonNumber': season, 'episodeNumber': episode,
            'hasFile': has_file, 'episodeFileId': eid * 10 if has_file else None,
            'airDateUtc': air}


# Silo-like: S3 fully downloaded, S3E3 is the finale.
S3 = [_ep(1, 3, 1, True), _ep(2, 3, 2, True), _ep(3, 3, 3, True)]


class _Resp:
    def __init__(self, ok=True, payload=None, status=200):
        self.ok, self._payload, self.status_code = ok, payload, status

    def json(self):
        return self._payload


def _run_finale(episodes, queue=None, queue_ok=True, rule=None):
    """Watch the S3 finale; return the episodes the finale gate released for deletion
    (ignores any ordinary keep-rule deletions in the same event)."""
    queue_resp = _Resp(ok=queue_ok, payload=queue or [], status=200 if queue_ok else 500)
    with mock.patch.object(mp, 'fetch_all_episodes', return_value=list(episodes)), \
         mock.patch.object(mp, 'update_activity_date'), \
         mock.patch.object(mp, 'unmonitor_episodes'), \
         mock.patch.object(mp, 'monitor_or_search_episodes'), \
         mock.patch.object(mp, 'fetch_next_episodes_dropdown', return_value=[]), \
         mock.patch.object(mp, 'delete_episodes_immediately') as delete, \
         mock.patch.object(mp, '_advance_sequential_if_finale'), \
         mock.patch.object(mp, '_unmonitor_if_series_ended'), \
         mock.patch.object(mp, 'is_anchor_episode', return_value=False), \
         mock.patch.object(mp, 'load_config', return_value={'rules': {}}), \
         mock.patch.object(mp, '_find_rule_name_for_series', return_value='testrule'), \
         mock.patch.object(mp.http, 'get', return_value=queue_resp):
        mp.process_episodes_for_webhook(
            series_id=42, season_number=3, episode_number=3,
            rule=dict(rule or RULE), series_title='Silo',
        )
    released = []
    for call in delete.call_args_list:
        if call.kwargs.get('reason', '').startswith('Season finale'):
            released.extend(call.args[0])
    return released


class FinaleReleaseTestCase(unittest.TestCase):
    def test_tba_next_season_is_released(self):
        # The reported Silo case: S4E1 exists, no file, no air date (TBA).
        eps = S3 + [_ep(4, 4, 1, False, air=None)]
        released = _run_finale(eps)
        self.assertEqual(sorted(e['id'] for e in released), [1, 2, 3])

    def test_future_dated_next_season_is_released(self):
        eps = S3 + [_ep(4, 4, 1, False, air='2027-06-01T00:00:00Z')]
        self.assertEqual(len(_run_finale(eps)), 3)

    def test_next_season_e1_with_file_is_kept(self):
        eps = S3 + [_ep(4, 4, 1, True), _ep(5, 4, 2, False)]
        self.assertEqual(_run_finale(eps), [])

    def test_next_season_e1_queued_is_kept(self):
        eps = S3 + [_ep(4, 4, 1, False)]
        self.assertEqual(_run_finale(eps, queue=[{'episodeId': 4}]), [])

    def test_queue_for_other_episode_does_not_count(self):
        eps = S3 + [_ep(4, 4, 1, False), _ep(5, 4, 2, False)]
        self.assertEqual(len(_run_finale(eps, queue=[{'episodeId': 5}])), 3)

    def test_queue_unreadable_keeps_protection(self):
        eps = S3 + [_ep(4, 4, 1, False)]
        self.assertEqual(_run_finale(eps, queue_ok=False), [])

    def test_ended_show_with_aired_fileless_later_seasons_is_released(self):
        # Past air dates, no files: previously returned "no next season" only by
        # accident of the date check; must still release under the new gate.
        eps = S3 + [_ep(4, 4, 1, False, air='2024-01-01T00:00:00Z'),
                    _ep(5, 5, 1, False, air='2024-06-01T00:00:00Z')]
        self.assertEqual(len(_run_finale(eps)), 3)

    def test_no_later_season_is_released(self):
        self.assertEqual(len(_run_finale(S3)), 3)

    def test_next_season_starting_above_e1_uses_lowest_episode(self):
        # No E1 in Sonarr; lowest episode > 0 (E2) has a file -> kept.
        eps = S3 + [_ep(7, 4, 2, True), _ep(8, 4, 3, False)]
        self.assertEqual(_run_finale(eps), [])

    def test_grace_watched_releases_without_deleting(self):
        eps = S3 + [_ep(4, 4, 1, False, air=None)]
        self.assertEqual(_run_finale(eps, rule={**RULE, 'grace_watched': 7}), [])


class NextEpisodeReadyTestCase(unittest.TestCase):
    def test_specials_and_episode_zero_never_count(self):
        eps = S3 + [_ep(9, 0, 1, True), _ep(6, 4, 0, True), _ep(7, 4, 1, False)]
        with mock.patch.object(mp.http, 'get', return_value=_Resp(payload=[])):
            ready, ep = mp._next_episode_ready(eps, 3, 42)
        self.assertFalse(ready)
        self.assertEqual(ep['id'], 7)

    def test_returns_first_episode_of_next_existing_season(self):
        eps = S3 + [_ep(8, 5, 2, False), _ep(7, 5, 1, True)]
        ready, ep = mp._next_episode_ready(eps, 3, 42)
        self.assertTrue(ready)
        self.assertEqual(ep['id'], 7)


if __name__ == '__main__':
    unittest.main()
