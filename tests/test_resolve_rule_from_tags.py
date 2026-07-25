"""
Tests for episeerr_utils.resolve_rule_from_tags() - shared tag-resolution
logic used by both the live Sonarr SeriesAdd webhook and reconcile.py's
episeerr_delay sweep. Self-contained stdlib unittest, run with:

    python3 -m unittest tests.test_resolve_rule_from_tags -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_IMPORT_TMPDIR = tempfile.mkdtemp(prefix='episeerr_rrft_import_')
os.environ.setdefault('LOG_DIR', _IMPORT_TMPDIR)
os.environ.setdefault('SETTINGS_DB_PATH', os.path.join(_IMPORT_TMPDIR, 'settings.db'))

from episeerr_utils import resolve_rule_from_tags


TAG_MAPPING = {
    1: 'episeerr_select',
    2: 'episeerr_delay',
    3: 'episeerr_default',
    4: 'episeerr_one_at_a_time',
    5: 'some_other_tag',
}

CONFIG = {
    'default_rule': 'one_at_a_time',
    'rules': {
        'default': {'series': {}},
        'one_at_a_time': {'series': {}},
    },
}


class ResolveRuleFromTagsTestCase(unittest.TestCase):
    def test_no_episeerr_tags_returns_nothing(self):
        rule, is_select = resolve_rule_from_tags([5], TAG_MAPPING, CONFIG)
        self.assertIsNone(rule)
        self.assertFalse(is_select)

    def test_select_tag_wins(self):
        rule, is_select = resolve_rule_from_tags([1, 2], TAG_MAPPING, CONFIG)
        self.assertIsNone(rule)
        self.assertTrue(is_select)

    def test_direct_rule_tag_matches_case_insensitively(self):
        config = {'default_rule': 'default', 'rules': {'One_At_A_Time': {'series': {}}}}
        tag_mapping = {4: 'episeerr_one_at_a_time'}
        rule, is_select = resolve_rule_from_tags([4], tag_mapping, config)
        self.assertEqual(rule, 'One_At_A_Time')
        self.assertFalse(is_select)

    def test_unknown_rule_tag_is_ignored(self):
        tag_mapping = {9: 'episeerr_nonexistent_rule'}
        rule, is_select = resolve_rule_from_tags([9], tag_mapping, CONFIG)
        self.assertIsNone(rule)
        self.assertFalse(is_select)

    def test_default_tag_resolves_through_config_default_rule_not_literal_name(self):
        # default_rule points at 'one_at_a_time', NOT a rule literally named
        # "default" - this is the whole point of the dynamic resolution.
        rule, is_select = resolve_rule_from_tags([3], TAG_MAPPING, CONFIG)
        self.assertEqual(rule, 'one_at_a_time')
        self.assertFalse(is_select)

    def test_default_tag_with_missing_default_rule_resolves_to_nothing(self):
        config = {'default_rule': 'does_not_exist', 'rules': {'default': {'series': {}}}}
        rule, is_select = resolve_rule_from_tags([3], TAG_MAPPING, config)
        self.assertIsNone(rule)
        self.assertFalse(is_select)

    def test_string_label_tags_are_supported(self):
        rule, is_select = resolve_rule_from_tags(['episeerr_one_at_a_time'], TAG_MAPPING, CONFIG)
        self.assertEqual(rule, 'one_at_a_time')

    def test_unrecognized_string_label_is_skipped_not_fatal(self):
        rule, is_select = resolve_rule_from_tags(['not_a_real_tag', 'episeerr_default'], TAG_MAPPING, CONFIG)
        self.assertEqual(rule, 'one_at_a_time')

    def test_select_checked_before_other_rule_tags_when_both_present(self):
        # episeerr_select at index 0 short-circuits before episeerr_default is reached
        rule, is_select = resolve_rule_from_tags([1, 3], TAG_MAPPING, CONFIG)
        self.assertTrue(is_select)
        self.assertIsNone(rule)


if __name__ == '__main__':
    unittest.main()
