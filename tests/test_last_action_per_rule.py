"""
activity_storage.get_last_search_per_rule(): per-rule "last activity" shown
on the Rules page (episeerr#115 follow-up - Rules as the living showcase).

    python3 -m unittest tests.test_last_action_per_rule -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix='episeerr_last_action_test_')
os.environ.setdefault('LOG_DIR', _TMP)

# activity_storage.py hardcodes ACTIVITY_DIR = '/app/data/activity' with no
# env override, and os.makedirs()'s it at import time - stub just that one
# call for the duration of the import so this runs outside the container too.
with mock.patch('os.makedirs'):
    import activity_storage  # noqa: E402


class LastActionPerRuleTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_searches_file = activity_storage.SEARCHES_FILE
        self._tmp_file = os.path.join(_TMP, f'searches_{id(self)}.json')
        activity_storage.SEARCHES_FILE = self._tmp_file

    def tearDown(self):
        activity_storage.SEARCHES_FILE = self._orig_searches_file
        if os.path.exists(self._tmp_file):
            os.remove(self._tmp_file)

    def _write_events(self, events):
        with open(self._tmp_file, 'w') as f:
            json.dump(events, f)

    def _config(self):
        return {'rules': {
            'jit': {'series': {'1': {}, '2': {}}},
            'binge': {'series': {'3': {}}},
        }}

    def test_picks_most_recent_per_rule(self):
        now = int(time.time())
        self._write_events([
            {'series_id': 1, 'series_title': 'Old Show', 'season': 1, 'episode': 1,
             'timestamp': now - 1000},
            {'series_id': 1, 'series_title': 'New Show A', 'season': 2, 'episode': 3,
             'timestamp': now},
            {'series_id': 3, 'series_title': 'Binge Show', 'season': 1, 'episode': 5,
             'timestamp': now - 50},
        ])
        result = activity_storage.get_last_search_per_rule(self._config())
        self.assertEqual(result['jit']['series_title'], 'New Show A')
        self.assertEqual(result['binge']['series_title'], 'Binge Show')
        self.assertIn('time_ago', result['jit'])

    def test_untracked_series_ignored(self):
        self._write_events([
            {'series_id': 999, 'series_title': 'Not in any rule', 'season': 1,
             'episode': 1, 'timestamp': int(time.time())},
        ])
        result = activity_storage.get_last_search_per_rule(self._config())
        self.assertEqual(result, {})

    def test_no_file_returns_empty(self):
        result = activity_storage.get_last_search_per_rule(self._config())
        self.assertEqual(result, {})

    def test_missing_config_returns_empty(self):
        self.assertEqual(activity_storage.get_last_search_per_rule(None), {})
        self.assertEqual(activity_storage.get_last_search_per_rule({}), {})


if __name__ == '__main__':
    unittest.main()
