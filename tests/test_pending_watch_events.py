"""
Tests for pending_watch_events.py (the manual-approval queue reconcile.py
feeds). Self-contained stdlib unittest, run with:

    python3 -m unittest tests.test_pending_watch_events -v
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pending_watch_events as pwe


class PendingWatchEventsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='episeerr_pwe_test_')
        pwe.PENDING_FILE = os.path.join(self.tmpdir, 'pending_watch_events.json')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_when_never_written(self):
        self.assertEqual(pwe.load_pending(), [])
        self.assertIsNone(pwe.get_last_checked())

    def test_add_creates_one_item(self):
        pwe.add_or_update_pending(series_id=10, series_title='Show', season=1,
                                   episode=2, source='plex', user='alice', watched_at=1000)
        items = pwe.load_pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['series_id'], 10)
        self.assertEqual(items[0]['season'], 1)
        self.assertEqual(items[0]['episode'], 2)
        self.assertIn('id', items[0])
        self.assertIn('detected_at', items[0])

    def test_second_add_for_same_series_bumps_forward_instead_of_duplicating(self):
        pwe.add_or_update_pending(series_id=10, series_title='Show', season=1,
                                   episode=2, source='plex', user='alice', watched_at=1000)
        pwe.add_or_update_pending(series_id=10, series_title='Show', season=1,
                                   episode=5, source='plex', user='alice', watched_at=2000)
        items = pwe.load_pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['episode'], 5)

    def test_add_with_older_episode_does_not_regress_existing_item(self):
        pwe.add_or_update_pending(series_id=10, series_title='Show', season=1,
                                   episode=5, source='plex', user='alice', watched_at=2000)
        pwe.add_or_update_pending(series_id=10, series_title='Show', season=1,
                                   episode=2, source='plex', user='alice', watched_at=1000)
        items = pwe.load_pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['episode'], 5)

    def test_different_series_are_independent_items(self):
        pwe.add_or_update_pending(series_id=10, series_title='Show A', season=1,
                                   episode=2, source='plex', user='alice', watched_at=1000)
        pwe.add_or_update_pending(series_id=20, series_title='Show B', season=1,
                                   episode=1, source='jellyfin', user='bob', watched_at=1500)
        self.assertEqual(len(pwe.load_pending()), 2)

    def test_get_item_returns_none_when_missing(self):
        self.assertIsNone(pwe.get_item('does-not-exist'))

    def test_clear_pending_removes_one_item_and_reports_found(self):
        pwe.add_or_update_pending(series_id=10, series_title='Show', season=1,
                                   episode=2, source='plex', user='alice', watched_at=1000)
        item_id = pwe.load_pending()[0]['id']
        self.assertTrue(pwe.clear_pending(item_id))
        self.assertEqual(pwe.load_pending(), [])

    def test_clear_pending_missing_item_reports_not_found(self):
        self.assertFalse(pwe.clear_pending('does-not-exist'))

    def test_clear_all_pending_empties_the_list(self):
        pwe.add_or_update_pending(series_id=10, series_title='Show A', season=1,
                                   episode=2, source='plex', user='alice', watched_at=1000)
        pwe.add_or_update_pending(series_id=20, series_title='Show B', season=1,
                                   episode=1, source='jellyfin', user='bob', watched_at=1500)
        pwe.clear_all_pending()
        self.assertEqual(pwe.load_pending(), [])

    def test_mark_checked_records_a_timestamp(self):
        pwe.mark_checked(12345)
        self.assertEqual(pwe.get_last_checked(), 12345)

    def test_mark_checked_defaults_to_now(self):
        import time
        before = int(time.time())
        pwe.mark_checked()
        self.assertGreaterEqual(pwe.get_last_checked(), before)

    def test_get_pending_summary_shape(self):
        pwe.add_or_update_pending(series_id=10, series_title='Show', season=1,
                                   episode=2, source='plex', user='alice', watched_at=1000)
        summary = pwe.get_pending_summary()
        self.assertEqual(summary['count'], 1)
        self.assertEqual(len(summary['items']), 1)


if __name__ == '__main__':
    unittest.main()
