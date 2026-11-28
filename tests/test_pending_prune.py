"""Stale pending-deletion entries get cleared (episeerr#106)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pending_deletions as pdm  # noqa: E402


def _queue(ep_id, file_id, series_id=1, season=1, number=1):
    pdm.queue_deletion(
        series_id=series_id, series_title='Show', season_number=season,
        episode_number=number, episode_id=ep_id, episode_file_id=file_id,
        episode_title='t', file_size=0, reason='r', date_source='d',
        date_value='v', rule_name='rule')


class PruneTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p1 = patch.object(pdm, 'PENDING_DELETIONS_FILE', os.path.join(self.tmp, 'p.json'))
        self.p2 = patch.object(pdm, 'is_episode_rejected', return_value=False)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()

    def _ids(self):
        return sorted(ep['episode_id'] for s in pdm.load_pending_deletions()
                      for sd in s['seasons'].values() for ep in sd['episodes'])

    def test_remove_drops_empty_series(self):
        _queue(10, 100)
        _queue(11, 101, number=2)
        self.assertEqual(pdm.remove_episodes_from_pending([10, 11]), 2)
        self.assertEqual(pdm.load_pending_deletions(), [])

    def test_prune_removes_missing_and_replaced_files_only(self):
        _queue(10, 100)               # file gone
        _queue(11, 101, number=2)     # file replaced (upgrade)
        _queue(12, 102, number=3)     # still there
        _queue(13, 103, number=4)     # episode gone from Sonarr
        sonarr = [
            {'id': 10, 'hasFile': False, 'episodeFileId': 0},
            {'id': 11, 'hasFile': True, 'episodeFileId': 999},
            {'id': 12, 'hasFile': True, 'episodeFileId': 102},
        ]
        removed = pdm.prune_stale_pending_deletions(lambda sid: sonarr)
        self.assertEqual(removed, 3)
        self.assertEqual(self._ids(), [12])

    def test_prune_leaves_everything_when_sonarr_returns_nothing(self):
        _queue(10, 100)
        self.assertEqual(pdm.prune_stale_pending_deletions(lambda sid: []), 0)
        self.assertEqual(self._ids(), [10])

    def test_prune_survives_fetch_errors(self):
        _queue(10, 100)

        def boom(sid):
            raise RuntimeError('down')
        self.assertEqual(pdm.prune_stale_pending_deletions(boom), 0)
        self.assertEqual(self._ids(), [10])


if __name__ == '__main__':
    unittest.main()
