"""
reconcile.check_unassigned_new_series(): auto-assign fallback for series whose
SeriesAdd webhook never reached Episeerr (episeerr#110).

    python3 -m unittest tests.test_auto_assign_sweep -v
"""

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_IMPORT_TMPDIR = tempfile.mkdtemp(prefix='episeerr_autoassign_import_')
os.environ.setdefault('LOG_DIR', _IMPORT_TMPDIR)
os.environ.setdefault('SETTINGS_DB_PATH', os.path.join(_IMPORT_TMPDIR, 'settings.db'))

import reconcile  # noqa: E402
import episeerr_utils  # noqa: E402


class FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


NOW = datetime.now(timezone.utc)
ENABLED_AT = NOW - timedelta(days=3)


class AutoAssignSweepTestCase(unittest.TestCase):
    def setUp(self):
        self._orig = sys.modules.get('media_processor')
        self.saved = []

    def tearDown(self):
        if self._orig is not None:
            sys.modules['media_processor'] = self._orig
        else:
            sys.modules.pop('media_processor', None)

    def _install(self, settings, config):
        mod = types.ModuleType('media_processor')
        mod.load_global_settings = lambda: settings
        mod.save_global_settings = lambda s: self.saved.append(dict(s))
        mod.load_config = lambda: config
        sys.modules['media_processor'] = mod

    def _settings(self, **kw):
        s = {'auto_assign_new_series': True, 'automation_held': False,
             'auto_assign_enabled_at': ENABLED_AT.isoformat()}
        s.update(kw)
        return s

    def _config(self, tracked=()):
        return {'default_rule': 'jit', 'rules': {
            'jit': {'series': {str(t): {} for t in tracked}},
            'other': {'series': {}},
        }}

    def _run(self, series_list, settings=None, config=None, rule=(None, False)):
        self._install(settings or self._settings(), config or self._config())
        with patch.object(episeerr_utils, 'get_sonarr_headers', return_value={}), \
             patch.object(episeerr_utils, 'get_tag_mapping', return_value={}), \
             patch.object(episeerr_utils.http, 'get', return_value=FakeResponse(series_list)), \
             patch.object(episeerr_utils, 'resolve_rule_from_tags', return_value=rule), \
             patch.object(episeerr_utils, 'cancel_queued_downloads_for_series') as cancel, \
             patch.object(episeerr_utils, 'unmonitor_series') as unmon, \
             patch.object(episeerr_utils, 'apply_initial_rule_selection', return_value=True) as apply, \
             patch.object(episeerr_utils, 'ensure_series_tracked_under_rule') as track:
            summary = reconcile.check_unassigned_new_series()
        return summary, cancel, unmon, apply, track

    def test_off_is_noop(self):
        summary, *_ , apply, track = self._run([], settings=self._settings(auto_assign_new_series=False))
        self.assertFalse(summary['ran'])

    def test_held_is_noop(self):
        summary, *_ = self._run([], settings=self._settings(automation_held=True))
        self.assertFalse(summary['ran'])

    def test_missing_enable_time_is_recorded_and_nothing_assigned(self):
        series = [{'id': 1, 'title': 'A', 'tags': [], 'added': _iso(NOW - timedelta(hours=1))}]
        summary, _, _, apply, track = self._run(series, settings=self._settings(auto_assign_enabled_at=None))
        self.assertEqual(summary['assigned'], 0)
        self.assertTrue(self.saved and self.saved[-1].get('auto_assign_enabled_at'))
        apply.assert_not_called()
        track.assert_not_called()

    def test_recent_untagged_series_gets_full_rule_applied(self):
        series = [{'id': 7, 'title': 'New', 'tags': [], 'added': _iso(NOW - timedelta(hours=1))}]
        summary, cancel, unmon, apply, track = self._run(series)
        self.assertEqual(summary['assigned'], 1)
        cancel.assert_called_once_with(7)
        unmon.assert_called_once()
        self.assertEqual(apply.call_args[0][0], 7)
        self.assertEqual(apply.call_args[0][2], 'jit')
        track.assert_not_called()

    def test_older_straggler_is_only_assigned(self):
        series = [{'id': 8, 'title': 'Older', 'tags': [], 'added': _iso(NOW - timedelta(days=2))}]
        summary, cancel, unmon, apply, track = self._run(series)
        self.assertEqual(summary['assigned'], 1)
        apply.assert_not_called()
        cancel.assert_not_called()
        self.assertEqual(track.call_args[0][:2], (8, 'jit'))

    def test_series_added_before_enable_is_left_alone(self):
        series = [{'id': 9, 'title': 'Old', 'tags': [], 'added': _iso(ENABLED_AT - timedelta(days=1))}]
        summary, _, _, apply, track = self._run(series)
        self.assertEqual(summary['assigned'], 0)
        apply.assert_not_called()
        track.assert_not_called()

    def test_already_tracked_series_is_left_alone(self):
        series = [{'id': 10, 'title': 'Tracked', 'tags': [], 'added': _iso(NOW - timedelta(hours=1))}]
        summary, _, _, apply, _ = self._run(series, config=self._config(tracked=[10]))
        self.assertEqual(summary['assigned'], 0)
        apply.assert_not_called()

    def test_tagged_series_is_left_to_webhook_paths(self):
        series = [{'id': 11, 'title': 'Tagged', 'tags': [3], 'added': _iso(NOW - timedelta(hours=1))}]
        summary, _, _, apply, track = self._run(series, rule=('other', False))
        self.assertEqual(summary['assigned'], 0)
        apply.assert_not_called()
        track.assert_not_called()

    def test_sonarr_seven_digit_fraction_parses(self):
        self.assertIsNotNone(reconcile._parse_ts('2026-11-16T10:53:23.1234567Z'))
        self.assertIsNotNone(reconcile._parse_ts('2026-11-16T10:53:23Z'))
        self.assertIsNotNone(reconcile._parse_ts(NOW.isoformat()))


if __name__ == '__main__':
    unittest.main()
