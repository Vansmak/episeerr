"""
Tests for issue #99: a watch event must never search Sonarr for episodes that
are already on disk, and the watch path never sends a season-pack search -
a 'seasons' rule gets the episodes left in the season as individual episodes
(season packs are only for newly added series).

Self-contained stdlib unittest, run with:

    python3 -m unittest tests.test_no_search_on_disk -v

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

_TMP = tempfile.mkdtemp(prefix='episeerr_issue99_test_')
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


def _ep(eid, season, episode, has_file):
    return {'id': eid, 'seasonNumber': season, 'episodeNumber': episode, 'hasFile': has_file}


class _Resp:
    def __init__(self, ok=True, payload=None, status=200):
        self.ok, self._payload, self.status_code = ok, payload, status

    def json(self):
        return self._payload


class EpisodesNeedingSearchTestCase(unittest.TestCase):
    def test_full_season_on_disk_returns_nothing(self):
        eps = [_ep(i, 1, i, True) for i in range(4, 7)]
        self.assertEqual(mp.episodes_needing_search(eps, [4, 5, 6]), [])

    def test_only_missing_in_requested_order(self):
        eps = [_ep(3, 1, 3, True), _ep(1, 1, 1, False), _ep(2, 1, 2, True), _ep(4, 1, 4, False)]
        self.assertEqual(mp.episodes_needing_search(eps, [1, 2, 3, 4]), [1, 4])


class TriggerSearchTestCase(unittest.TestCase):
    def _trigger(self, episodes, ids, get_type, get_ok=True):
        get_resp = _Resp(ok=get_ok, payload=episodes, status=200 if get_ok else 500)
        with mock.patch.object(mp.http, 'get', return_value=get_resp), \
             mock.patch.object(mp.http, 'post', return_value=_Resp()) as post, \
             mock.patch.object(mp, 'get_episode_details_by_id', return_value=None):
            mp.trigger_episode_search_in_sonarr(ids, 42, None, get_type)
        return [c.kwargs['json'] for c in post.call_args_list]

    def test_no_command_when_everything_has_file(self):
        eps = [_ep(i, 1, i, True) for i in range(4, 7)]
        self.assertEqual(self._trigger(eps, [4, 5, 6], 'seasons'), [])
        self.assertEqual(self._trigger(eps, [4, 5, 6], 'episodes'), [])

    def test_seasons_rule_mid_season_searches_remaining_episodes_individually(self):
        # Watched E5 of 10, nothing after it downloaded yet: individual
        # episodes, never a season pack, on the watch path.
        eps = [_ep(i, 1, i, False) for i in range(6, 11)]
        self.assertEqual(self._trigger(eps, [6, 7, 8, 9, 10], 'seasons'),
                         [{'name': 'EpisodeSearch', 'episodeIds': [6, 7, 8, 9, 10]}])

    def test_seasons_rule_next_season_after_finale_is_not_a_pack(self):
        eps = [_ep(20, 2, 1, False), _ep(21, 2, 2, False)]
        self.assertEqual(self._trigger(eps, [20, 21], 'seasons'),
                         [{'name': 'EpisodeSearch', 'episodeIds': [20, 21]}])

    def test_partial_season_searches_only_gaps(self):
        eps = [_ep(4, 1, 4, True), _ep(5, 1, 5, False), _ep(6, 1, 6, True)]
        for get_type in ('seasons', 'episodes'):
            self.assertEqual(self._trigger(eps, [4, 5, 6], get_type),
                             [{'name': 'EpisodeSearch', 'episodeIds': [5]}])

    def test_lookup_failure_falls_back_to_episode_search(self):
        self.assertEqual(self._trigger(None, [4, 5], 'seasons', get_ok=False),
                         [{'name': 'EpisodeSearch', 'episodeIds': [4, 5]}])


if __name__ == '__main__':
    unittest.main()
