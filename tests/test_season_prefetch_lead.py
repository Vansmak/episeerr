"""
Tests for issue #105 follow-up: season_prefetch_lead lets a get_type='seasons'
rule start fetching the next season while N episodes still remain in the
current one, instead of only once the season is fully watched.

Self-contained stdlib unittest, run with:

    python3 -m unittest tests.test_season_prefetch_lead -v
"""

import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix='episeerr_issue105_prefetch_test_')
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


def _ep(eid, season, episode):
    return {'id': eid, 'seasonNumber': season, 'episodeNumber': episode}


# Season 1: episodes 101-105 (5 eps). Season 2: episodes 201-203 (3 eps).
_SEASONS = {
    1: [_ep(100 + n, 1, n) for n in range(1, 6)],
    2: [_ep(200 + n, 2, n) for n in range(1, 4)],
}


class _Resp:
    def __init__(self, payload):
        self.ok = True
        self._payload = payload

    def json(self):
        return self._payload


def _fake_get(url, headers=None):
    m = re.search(r'seasonNumber=(\d+)', url)
    season = int(m.group(1)) if m else None
    return _Resp(_SEASONS.get(season, []))


class SeasonPrefetchLeadTestCase(unittest.TestCase):
    def _dropdown(self, episode_number, lead, get_count=1):
        with mock.patch.object(mp.http, 'get', side_effect=_fake_get):
            return mp.fetch_next_episodes_dropdown(
                series_id=1, season_number=1, episode_number=episode_number,
                get_type='seasons', get_count=get_count, season_prefetch_lead=lead,
            )

    def test_default_lead_zero_waits_for_finale(self):
        # Watched E3 of 5: 2 remain, lead=0 -> only the remaining current-season eps.
        ids = self._dropdown(episode_number=3, lead=0)
        self.assertEqual(ids, [104, 105])

    def test_lead_matches_remaining_triggers_next_season(self):
        # Watched E3 of 5: 2 remain, lead=2 -> next season pulled in too.
        ids = self._dropdown(episode_number=3, lead=2)
        self.assertEqual(ids, [104, 105, 201, 202, 203])

    def test_lead_greater_than_remaining_still_triggers(self):
        ids = self._dropdown(episode_number=3, lead=5)
        self.assertEqual(ids, [104, 105, 201, 202, 203])

    def test_lead_less_than_remaining_does_not_trigger_yet(self):
        # Watched E2 of 5: 3 remain, lead=2 -> not yet.
        ids = self._dropdown(episode_number=2, lead=2)
        self.assertEqual(ids, [103, 104, 105])

    def test_watching_actual_finale_still_triggers_regardless_of_lead(self):
        ids = self._dropdown(episode_number=5, lead=0)
        self.assertEqual(ids, [201, 202, 203])

    def test_lead_zero_is_default_when_omitted(self):
        with mock.patch.object(mp.http, 'get', side_effect=_fake_get):
            ids = mp.fetch_next_episodes_dropdown(
                series_id=1, season_number=1, episode_number=3,
                get_type='seasons', get_count=1,
            )
        self.assertEqual(ids, [104, 105])


if __name__ == '__main__':
    unittest.main()
