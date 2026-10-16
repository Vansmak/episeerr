"""
Tests for the 'c' (current season) always_have token - the "Premiere" rule
pattern from GitHub discussion #102: dangle the newest season's premiere
until watched, then hand off to normal sequential advance.

Self-contained stdlib unittest, run with:

    python3 -m unittest tests.test_current_season_token -v

media_processor's external deps (Flask app, settings DB, HTTP) are stubbed only
for the duration of its import, via patch.dict on sys.modules, same pattern as
test_finale_release.py.
"""

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix='episeerr_current_season_test_')
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


def _ep(season, episode, days_from_now=None, has_file=False):
    """Build a minimal Sonarr episode dict. days_from_now negative = past."""
    ep = {'seasonNumber': season, 'episodeNumber': episode, 'hasFile': has_file,
          'id': season * 100 + episode}
    if days_from_now is not None:
        air = datetime.now(timezone.utc) + timedelta(days=days_from_now)
        ep['airDateUtc'] = air.strftime('%Y-%m-%dT%H:%M:%SZ')
    return ep


class ResolveCurrentSeasonTestCase(unittest.TestCase):
    def test_picks_highest_aired_season(self):
        episodes = [
            _ep(1, 1, days_from_now=-400, has_file=True),
            _ep(2, 1, days_from_now=-200, has_file=True),
            _ep(3, 1, days_from_now=-10, has_file=False),
        ]
        self.assertEqual(mp.resolve_current_season(episodes), 3)

    def test_ignores_announced_but_unaired_season(self):
        episodes = [
            _ep(5, 1, days_from_now=-5, has_file=True),
            _ep(6, 1, days_from_now=30, has_file=False),  # announced, future
        ]
        self.assertEqual(mp.resolve_current_season(episodes), 5)

    def test_ignores_specials_season_zero(self):
        episodes = [
            _ep(0, 1, days_from_now=-5, has_file=True),
            _ep(1, 1, days_from_now=-3, has_file=True),
        ]
        self.assertEqual(mp.resolve_current_season(episodes), 1)

    def test_none_when_nothing_has_aired(self):
        episodes = [_ep(1, 1, days_from_now=10)]
        self.assertIsNone(mp.resolve_current_season(episodes))

    def test_episode_with_no_air_date_is_ignored(self):
        episodes = [_ep(1, 1, days_from_now=-5, has_file=True), {'seasonNumber': 2, 'episodeNumber': 1}]
        self.assertEqual(mp.resolve_current_season(episodes), 1)


class ResolveCTokensTestCase(unittest.TestCase):
    def test_substitutes_bare_sc(self):
        self.assertEqual(mp.resolve_c_tokens('sc', 5), 's5')

    def test_substitutes_sce1_with_modifiers(self):
        self.assertEqual(mp.resolve_c_tokens('sce1+', 5), 's5e1+')
        self.assertEqual(mp.resolve_c_tokens('sce1-', 5), 's5e1-')

    def test_leaves_other_tokens_untouched(self):
        self.assertEqual(mp.resolve_c_tokens('s1, sce1', 5), 's1,s5e1')

    def test_drops_c_part_when_nothing_has_aired(self):
        self.assertEqual(mp.resolve_c_tokens('sce1', None), '')
        self.assertEqual(mp.resolve_c_tokens('s1, sce1', None), 's1')

    def test_no_c_token_returns_expression_unchanged(self):
        self.assertEqual(mp.resolve_c_tokens('s1, s*e1', 5), 's1, s*e1')

    def test_episode_range_on_current_season(self):
        self.assertEqual(mp.resolve_c_tokens('sce1-3', 5), 's5e1-3')


class ParseAlwaysHaveHasCTestCase(unittest.TestCase):
    def test_sce1_sets_has_c_and_activation_ep(self):
        parsed = mp.parse_always_have('sce1')
        self.assertTrue(parsed['has_c'])
        self.assertEqual(parsed['activation_ep'], 1)
        self.assertFalse(parsed['is_sequential'])  # starts with 's', not 'e'

    def test_bare_sc_sets_has_c_no_activation_ep(self):
        parsed = mp.parse_always_have('sc')
        self.assertTrue(parsed['has_c'])
        self.assertIsNone(parsed['activation_ep'])

    def test_plain_season_token_does_not_set_has_c(self):
        parsed = mp.parse_always_have('s4e1')
        self.assertFalse(parsed['has_c'])

    def test_validation_accepts_c_token_shapes(self):
        for expr in ('sc', 'sce1', 'sce1-', 'sce1+', 'sce1-3'):
            is_valid, err = mp.validate_always_have_expression(expr)
            self.assertTrue(is_valid, f"{expr!r} should be valid, got error: {err}")


class IsAnchorEpisodeNeverProtectsCTestCase(unittest.TestCase):
    def test_c_token_never_anchors_even_while_held(self):
        config = {
            'rules': {
                'premiere': {
                    'always_have': 'sce1',
                    'series': {
                        '42': {'activation_seasons': {'5': 'premiere_held'}},
                    },
                }
            }
        }
        with mock.patch.object(mp, 'load_config', return_value=config):
            episode = {'seasonNumber': 5, 'episodeNumber': 1}
            self.assertFalse(mp.is_anchor_episode(episode, series_id=42))


if __name__ == '__main__':
    unittest.main()
