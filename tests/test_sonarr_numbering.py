"""
Tests for sonarr_utils.format_seasons() and format_season_episodes(). Self-contained
stdlib unittest, run with:

    python3 -m unittest tests.test_sonarr_numbering -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_IMPORT_TMPDIR = tempfile.mkdtemp(prefix='episeerr_sn_import_')
os.environ.setdefault('LOG_DIR', _IMPORT_TMPDIR)
os.environ.setdefault('SETTINGS_DB_PATH', os.path.join(_IMPORT_TMPDIR, 'settings.db'))

from sonarr_utils import format_seasons, format_season_episodes


HXH_SERIES = {
    'title': 'Hunter x Hunter (2011)',
    'seasons': [
        {'seasonNumber': 0, 'statistics': {'totalEpisodeCount': 2}},
        {'seasonNumber': 1, 'statistics': {'totalEpisodeCount': 58}},
        {'seasonNumber': 2, 'statistics': {'totalEpisodeCount': 78}},
        {'seasonNumber': 3, 'statistics': {'totalEpisodeCount': 12}},
    ],
}


class FormatSeasonsTestCase(unittest.TestCase):
    def test_sonarr_seasons_without_specials(self):
        self.assertEqual(format_seasons(HXH_SERIES), [
            {'seasonNumber': 1, 'episodeCount': 58},
            {'seasonNumber': 2, 'episodeCount': 78},
            {'seasonNumber': 3, 'episodeCount': 12},
        ])

    def test_unknown_count_right_after_adding(self):
        series = {'seasons': [{'seasonNumber': 1}, {'seasonNumber': 2, 'statistics': {}}]}
        self.assertEqual(format_seasons(series), [
            {'seasonNumber': 1, 'episodeCount': '?'},
            {'seasonNumber': 2, 'episodeCount': '?'},
        ])

    def test_series_lookup_without_statistics(self):
        # Discover/Search: /series/lookup for a show not yet in Sonarr has
        # Sonarr's season numbers but no per-season statistics.
        lookup = {'title': 'Hunter x Hunter (2011)', 'seasons': [
            {'seasonNumber': 0, 'monitored': False},
            {'seasonNumber': 1, 'monitored': True},
            {'seasonNumber': 2, 'monitored': True},
            {'seasonNumber': 3, 'monitored': True},
        ]}
        self.assertEqual(format_seasons(lookup), [
            {'seasonNumber': 1, 'episodeCount': '?'},
            {'seasonNumber': 2, 'episodeCount': '?'},
            {'seasonNumber': 3, 'episodeCount': '?'},
        ])

    def test_lookup_with_only_specials_is_empty(self):
        # Caller falls back to TMDB when nothing usable comes back.
        self.assertEqual(format_seasons({'seasons': [{'seasonNumber': 0}]}), [])
        self.assertEqual(format_seasons({}), [])


class FormatSeasonEpisodesTestCase(unittest.TestCase):
    def test_sonarr_numbers_with_absolute_number(self):
        episodes = [
            {'seasonNumber': 2, 'episodeNumber': 73, 'absoluteEpisodeNumber': 131,
             'title': 'Anger x And x Light', 'overview': None, 'airDate': '2014-05-28'},
            {'seasonNumber': 2, 'episodeNumber': 72, 'absoluteEpisodeNumber': 130,
             'title': 'Magic x to x Destroy', 'overview': 'Gon ...', 'airDate': '2014-05-21'},
        ]
        result = format_season_episodes(episodes)
        self.assertEqual([e['episode_number'] for e in result], [72, 73])
        self.assertEqual([e['absolute_episode_number'] for e in result], [130, 131])
        self.assertEqual(result[0]['name'], 'Magic x to x Destroy')
        self.assertEqual(result[1]['overview'], '')

    def test_missing_title_and_absolute_number(self):
        result = format_season_episodes([{'seasonNumber': 1, 'episodeNumber': 3}])
        self.assertEqual(result, [{
            'episode_number': 3, 'absolute_episode_number': None,
            'name': 'Episode 3', 'overview': '', 'air_date': None,
        }])


if __name__ == '__main__':
    unittest.main()
