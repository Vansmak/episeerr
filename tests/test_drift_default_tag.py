"""
Regression tests for the episeerr_default resolution bug in
validate_series_tag() / reconcile_series_drift(): both used to match the
literal tag suffix ("default") against a rule name, instead of resolving
through config['default_rule'] like resolve_rule_from_tags() does - so a
renamed default rule (config['default_rule'] != 'default') was silently
unrecognized by drift detection and orphan recovery, even though the live
webhook and the episeerr_delay sweep handled it correctly.

Self-contained stdlib unittest, run with:
    python3 -m unittest tests.test_drift_default_tag -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_IMPORT_TMPDIR = tempfile.mkdtemp(prefix='episeerr_drift_import_')
os.environ.setdefault('LOG_DIR', _IMPORT_TMPDIR)
os.environ.setdefault('SETTINGS_DB_PATH', os.path.join(_IMPORT_TMPDIR, 'settings.db'))

import episeerr_utils

# Series carries only the episeerr_default tag (id 3); default_rule points
# at a renamed rule, not one literally called "default".
RENAMED_DEFAULT_CONFIG = {
    'default_rule': 'one_at_a_time',
    'rules': {'one_at_a_time': {'series': {}}},
}
TAG_MAPPING = {3: 'episeerr_default'}


class ValidateSeriesTagDefaultResolutionTestCase(unittest.TestCase):
    def test_without_config_falls_back_to_literal_default_match(self):
        series_data = {'tags': [3]}
        with patch.object(episeerr_utils, 'get_tag_mapping', return_value=TAG_MAPPING):
            matches, actual = episeerr_utils.validate_series_tag(
                1, 'one_at_a_time', series_data=series_data
            )
        # No config passed -> can't resolve default_rule -> literal 'default'
        # doesn't match 'one_at_a_time'. Documents the fallback, not the fix.
        self.assertFalse(matches)
        self.assertEqual(actual, 'default')

    def test_with_config_resolves_default_tag_to_current_default_rule(self):
        series_data = {'tags': [3]}
        with patch.object(episeerr_utils, 'get_tag_mapping', return_value=TAG_MAPPING):
            matches, actual = episeerr_utils.validate_series_tag(
                1, 'one_at_a_time', series_data=series_data, config=RENAMED_DEFAULT_CONFIG
            )
        self.assertTrue(matches)
        self.assertEqual(actual, 'one_at_a_time')


class ReconcileSeriesDriftOrphanRecoveryTestCase(unittest.TestCase):
    def test_orphaned_default_tag_recovers_to_renamed_default_rule(self):
        config = {
            'default_rule': 'one_at_a_time',
            'rules': {'one_at_a_time': {'series': {}}},
        }
        series_data = {'tags': [3]}
        with patch.object(episeerr_utils, 'get_tag_mapping', return_value=TAG_MAPPING):
            rule, modified = episeerr_utils.reconcile_series_drift(
                42, config, series_data=series_data
            )
        self.assertEqual(rule, 'one_at_a_time')
        self.assertTrue(modified)
        self.assertIn('42', config['rules']['one_at_a_time']['series'])

    def test_orphaned_default_tag_with_no_matching_rule_at_all_gives_up_cleanly(self):
        config = {'default_rule': 'does_not_exist', 'rules': {'one_at_a_time': {'series': {}}}}
        series_data = {'tags': [3]}
        with patch.object(episeerr_utils, 'get_tag_mapping', return_value=TAG_MAPPING):
            rule, modified = episeerr_utils.reconcile_series_drift(
                42, config, series_data=series_data
            )
        self.assertIsNone(rule)
        self.assertFalse(modified)

    def test_already_tracked_series_with_default_tag_is_recognized_as_matching(self):
        # Series IS already tracked under one_at_a_time; Sonarr still shows
        # episeerr_default (not yet swapped). Before the fix this would have
        # been flagged as drift (literal 'default' != 'one_at_a_time').
        config = {
            'default_rule': 'one_at_a_time',
            'rules': {'one_at_a_time': {'series': {'42': {'activity_date': 123}}}},
        }
        series_data = {'tags': [3]}
        with patch.object(episeerr_utils, 'get_tag_mapping', return_value=TAG_MAPPING), \
             patch.object(episeerr_utils, 'sync_rule_tag_to_sonarr') as mock_sync:
            rule, modified = episeerr_utils.reconcile_series_drift(
                42, config, series_data=series_data
            )
        self.assertEqual(rule, 'one_at_a_time')
        self.assertFalse(modified)
        mock_sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
