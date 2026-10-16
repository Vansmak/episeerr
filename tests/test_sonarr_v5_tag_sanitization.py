"""
Tests for episeerr#104: Sonarr v5 rejects tag labels with '_' or uppercase
(^[a-z0-9-]+$ only). These cover the pure sanitization/matching helpers;
migrate_legacy_episeerr_tags()/get_or_create_rule_tag_id() themselves are
HTTP-heavy and covered by the manual repro steps in the issue instead.

Self-contained stdlib unittest, run with:

    python3 -m unittest tests.test_sonarr_v5_tag_sanitization -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_IMPORT_TMPDIR = tempfile.mkdtemp(prefix='episeerr_tagv5_import_')
os.environ.setdefault('LOG_DIR', _IMPORT_TMPDIR)
os.environ.setdefault('SETTINGS_DB_PATH', os.path.join(_IMPORT_TMPDIR, 'settings.db'))

from episeerr_utils import sanitize_tag_label, is_episeerr_tag, strip_episeerr_prefix


class SanitizeTagLabelTestCase(unittest.TestCase):
    def test_underscore_rule_name_becomes_hyphenated(self):
        self.assertEqual(sanitize_tag_label('one_at_a_time'), 'episeerr-one-at-a-time')

    def test_uppercase_rule_name_is_lowercased(self):
        self.assertEqual(sanitize_tag_label('Showcase'), 'episeerr-showcase')

    def test_mixed_case_and_underscore(self):
        self.assertEqual(sanitize_tag_label('Ended_unmonitor'), 'episeerr-ended-unmonitor')

    def test_already_clean_rule_name_unchanged(self):
        self.assertEqual(sanitize_tag_label('finished'), 'episeerr-finished')

    def test_matches_sonarr_v5_regex(self):
        import re
        for name in ('one_at_a_time', 'Get1keepseason', 'KeepAll', 'ForSmoochy', 'Showcase'):
            label = sanitize_tag_label(name)
            self.assertRegex(label, r'^[a-z0-9-]+$', f"{label!r} (from {name!r}) violates Sonarr v5's tag regex")

    def test_no_double_hyphens_from_adjacent_special_chars(self):
        self.assertEqual(sanitize_tag_label('a__b'), 'episeerr-a-b')


class IsEpiseerrTagTestCase(unittest.TestCase):
    def test_legacy_underscore_prefix(self):
        self.assertTrue(is_episeerr_tag('episeerr_showcase'))

    def test_new_hyphen_prefix(self):
        self.assertTrue(is_episeerr_tag('episeerr-showcase'))

    def test_unrelated_tag_is_not_episeerr(self):
        self.assertFalse(is_episeerr_tag('anime'))

    def test_case_insensitive(self):
        self.assertTrue(is_episeerr_tag('EPISEERR_SHOWCASE'))

    def test_empty_or_none(self):
        self.assertFalse(is_episeerr_tag(''))
        self.assertFalse(is_episeerr_tag(None))


class StripEpiseerrPrefixTestCase(unittest.TestCase):
    def test_strips_legacy_prefix(self):
        self.assertEqual(strip_episeerr_prefix('episeerr_showcase'), 'showcase')

    def test_strips_new_prefix(self):
        self.assertEqual(strip_episeerr_prefix('episeerr-showcase'), 'showcase')

    def test_non_episeerr_tag_returns_empty(self):
        self.assertEqual(strip_episeerr_prefix('anime'), '')


if __name__ == '__main__':
    unittest.main()
