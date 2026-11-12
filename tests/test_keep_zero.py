"""
Tests that keep_count=0 for keep_type='seasons' means "keep nothing"
(immediate delete after watching, matching the UI's own explanation text),
not "keep 1" - `seasons_to_keep = keep_count if keep_count else 1` treated an
explicit 0 as falsy and silently kept the current season regardless.

Self-contained stdlib unittest, run with:

    python3 -m unittest tests.test_keep_zero -v
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix='episeerr_keep_zero_test_')
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
    sys.modules.pop('media_processor', None)
    if saved is not None:
        sys.modules['media_processor'] = saved
    return mod


mp = _load_media_processor()


def _ep(eid, season, episode, has_file=True):
    return {'id': eid, 'seasonNumber': season, 'episodeNumber': episode, 'hasFile': has_file}


# Season 1 (watched, has files) and season 2 (current, just watched E1, has file).
_EPISODES = [
    _ep(101, 1, 1), _ep(102, 1, 2),
    _ep(201, 2, 1),
]


class KeepZeroSeasonsTestCase(unittest.TestCase):
    def test_keep_zero_deletes_current_season_too(self):
        leaving = mp.find_episodes_leaving_keep_block(
            _EPISODES, 'seasons', 0, last_watched_season=2, last_watched_episode=1
        )
        leaving_ids = {ep['id'] for ep in leaving}
        self.assertEqual(leaving_ids, {101, 102, 201})

    def test_keep_one_protects_current_season_only(self):
        leaving = mp.find_episodes_leaving_keep_block(
            _EPISODES, 'seasons', 1, last_watched_season=2, last_watched_episode=1
        )
        leaving_ids = {ep['id'] for ep in leaving}
        self.assertEqual(leaving_ids, {101, 102})

    def test_keep_none_defaults_to_one(self):
        # Legacy rules with no keep_count stored at all should still default
        # to keeping 1 season, same as before this fix.
        leaving = mp.find_episodes_leaving_keep_block(
            _EPISODES, 'seasons', None, last_watched_season=2, last_watched_episode=1
        )
        leaving_ids = {ep['id'] for ep in leaving}
        self.assertEqual(leaving_ids, {101, 102})


if __name__ == '__main__':
    unittest.main()
