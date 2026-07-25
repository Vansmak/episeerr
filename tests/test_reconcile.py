"""
Tests for reconcile.py (missed watch-event detection). Self-contained
stdlib unittest - no pytest dependency, run with:

    python3 -m unittest tests.test_reconcile -v

media_processor.py (and transitively episeerr.py) is not imported here -
reconcile.check_for_missed_watch_events() only imports it lazily inside the
function, so tests install a fake 'media_processor' module and a fake
'pending_watch_events' module in sys.modules instead of pulling in the real
Flask app / real file-backed queue just to exercise the detection logic.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# episeerr_utils -> logging_config creates LOG_DIR at import time (defaults
# to /app/logs), and -> settings_db opens SETTINGS_DB_PATH at import time;
# point both at a scratch dir before import so that succeeds outside the
# container.
_IMPORT_TMPDIR = tempfile.mkdtemp(prefix='episeerr_reconcile_import_')
os.environ.setdefault('LOG_DIR', _IMPORT_TMPDIR)
os.environ.setdefault('SETTINGS_DB_PATH', os.path.join(_IMPORT_TMPDIR, 'settings.db'))

import reconcile
import episeerr_utils


def _fake_media_processor(settings, get_series_id=None, config=None):
    module = types.ModuleType('media_processor')
    module.load_global_settings = lambda: settings
    module.get_series_id = get_series_id or (lambda title: None)
    module.load_config = lambda: config or {'rules': {}}
    return module


class IsNewerThanRecordedTestCase(unittest.TestCase):
    def test_series_untracked_by_any_rule_is_not_our_concern(self):
        config = {'rules': {'Standard': {'series': {}}}}
        self.assertFalse(reconcile._is_newer_than_recorded(10, 1, 5, config))

    def test_no_last_episode_recorded_is_newer(self):
        config = {'rules': {'Standard': {'series': {'10': {'activity_date': 123}}}}}
        self.assertTrue(reconcile._is_newer_than_recorded(10, 1, 5, config))

    def test_per_series_scope_compares_season_and_episode_tuple(self):
        config = {'rules': {'Standard': {'series': {
            '10': {'last_season': 1, 'last_episode': 5}
        }}}}
        self.assertTrue(reconcile._is_newer_than_recorded(10, 1, 6, config))
        self.assertTrue(reconcile._is_newer_than_recorded(10, 2, 1, config))
        self.assertFalse(reconcile._is_newer_than_recorded(10, 1, 5, config))
        self.assertFalse(reconcile._is_newer_than_recorded(10, 1, 4, config))

    def test_per_season_scope_compares_within_that_season_only(self):
        config = {'rules': {'Standard': {
            'grace_scope': 'season',
            'series': {'10': {'seasons': {'1': {'last_episode': 5}}}},
        }}}
        self.assertTrue(reconcile._is_newer_than_recorded(10, 1, 6, config))
        self.assertFalse(reconcile._is_newer_than_recorded(10, 1, 5, config))
        # season 2 never tracked yet -> newer
        self.assertTrue(reconcile._is_newer_than_recorded(10, 2, 1, config))


class CheckForMissedWatchEventsTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_media_processor = sys.modules.get('media_processor')
        self._orig_pending = sys.modules.get('pending_watch_events')

    def tearDown(self):
        for name, orig in (('media_processor', self._orig_media_processor),
                           ('pending_watch_events', self._orig_pending)):
            if orig is not None:
                sys.modules[name] = orig
            else:
                sys.modules.pop(name, None)

    def _install_fake_pending_module(self):
        calls = []
        module = types.ModuleType('pending_watch_events')
        module.add_or_update_pending = lambda **kwargs: calls.append(kwargs)
        module.mark_checked = lambda: calls.append({'marked_checked': True})
        sys.modules['pending_watch_events'] = module
        return calls

    def test_disabled_is_a_noop(self):
        sys.modules['media_processor'] = _fake_media_processor({'reconcile_enabled': False})
        calls = self._install_fake_pending_module()
        summary = reconcile.check_for_missed_watch_events()
        self.assertFalse(summary['ran'])
        self.assertEqual(calls, [])

    def test_held_automation_is_a_noop(self):
        sys.modules['media_processor'] = _fake_media_processor(
            {'reconcile_enabled': True, 'automation_held': True})
        calls = self._install_fake_pending_module()
        summary = reconcile.check_for_missed_watch_events()
        self.assertFalse(summary['ran'])
        self.assertEqual(calls, [])

    def test_finds_newer_event_and_queues_it(self):
        config = {'rules': {'Standard': {'series': {'42': {'last_season': 1, 'last_episode': 1}}}}}
        sys.modules['media_processor'] = _fake_media_processor(
            {'reconcile_enabled': True},
            get_series_id=lambda title: 42,
            config=config,
        )
        calls = self._install_fake_pending_module()

        with patch.object(reconcile, '_SWEEPERS', (
            ('plex', lambda: [(2000, 'Show', 1, 2, 'alice')]),
        )):
            summary = reconcile.check_for_missed_watch_events()

        self.assertTrue(summary['ran'])
        self.assertEqual(summary['found'], 1)
        queued = [c for c in calls if 'series_id' in c]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]['series_id'], 42)
        self.assertEqual(queued[0]['season'], 1)
        self.assertEqual(queued[0]['episode'], 2)

    def test_event_not_newer_than_recorded_is_skipped(self):
        config = {'rules': {'Standard': {'series': {'42': {'last_season': 2, 'last_episode': 5}}}}}
        sys.modules['media_processor'] = _fake_media_processor(
            {'reconcile_enabled': True},
            get_series_id=lambda title: 42,
            config=config,
        )
        calls = self._install_fake_pending_module()

        with patch.object(reconcile, '_SWEEPERS', (
            ('plex', lambda: [(2000, 'Show', 1, 1, 'alice')]),
        )):
            summary = reconcile.check_for_missed_watch_events()

        self.assertEqual(summary['found'], 0)
        self.assertEqual([c for c in calls if 'series_id' in c], [])

    def test_series_not_managed_by_sonarr_is_skipped(self):
        sys.modules['media_processor'] = _fake_media_processor(
            {'reconcile_enabled': True}, get_series_id=lambda title: None,
        )
        calls = self._install_fake_pending_module()

        with patch.object(reconcile, '_SWEEPERS', (
            ('plex', lambda: [(2000, 'Unmanaged Show', 1, 1, 'alice')]),
        )):
            summary = reconcile.check_for_missed_watch_events()

        self.assertEqual(summary['found'], 0)
        self.assertEqual([c for c in calls if 'series_id' in c], [])

    def test_one_source_failing_does_not_block_the_others(self):
        config = {'rules': {'Standard': {'series': {'42': {'last_season': 1, 'last_episode': 1}}}}}
        sys.modules['media_processor'] = _fake_media_processor(
            {'reconcile_enabled': True}, get_series_id=lambda title: 42, config=config,
        )
        calls = self._install_fake_pending_module()

        def broken(*a):
            raise RuntimeError('history API down')

        with patch.object(reconcile, '_SWEEPERS', (
            ('plex', broken),
            ('jellyfin', lambda: [(2000, 'Show', 1, 2, 'bob')]),
        )):
            summary = reconcile.check_for_missed_watch_events()

        self.assertEqual(summary['found'], 1)
        self.assertTrue(summary['errors'])

    def test_always_marks_checked_even_when_nothing_found(self):
        sys.modules['media_processor'] = _fake_media_processor(
            {'reconcile_enabled': True}, get_series_id=lambda title: None,
        )
        calls = self._install_fake_pending_module()

        with patch.object(reconcile, '_SWEEPERS', ()):
            reconcile.check_for_missed_watch_events()

        self.assertIn({'marked_checked': True}, calls)


class ReplayWatchEventTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_tautulli = sys.modules.get('integrations.tautulli')
        self._orig_integrations = sys.modules.get('integrations')

    def tearDown(self):
        for name, orig in (('integrations.tautulli', self._orig_tautulli),
                           ('integrations', self._orig_integrations)):
            if orig is not None:
                sys.modules[name] = orig
            else:
                sys.modules.pop(name, None)

    def test_tautulli_routes_through_process_watch_event_not_process_episode(self):
        calls = []

        def fake_process_watch_event(data):
            calls.append(data)
            return {'status': 'success'}

        fake_module = types.ModuleType('integrations.tautulli')
        fake_module.process_watch_event = fake_process_watch_event
        with patch.dict(sys.modules, {'integrations.tautulli': fake_module}):
            result = reconcile.replay_watch_event('tautulli', 'Some Show', 2, 5, 'alice')

        self.assertTrue(result)
        self.assertEqual(calls, [{
            'plex_title': 'Some Show', 'plex_season_num': 2, 'plex_ep_num': 5,
        }])

    def test_other_sources_route_through_process_episode(self):
        captured = {}

        class FakeIntegration:
            def process_episode(self, episode_info):
                captured.update(episode_info)
                return True

        fake_integrations_module = types.ModuleType('integrations')
        fake_integrations_module.get_integration = lambda name: FakeIntegration()
        with patch.dict(sys.modules, {'integrations': fake_integrations_module}):
            result = reconcile.replay_watch_event('plex', 'Some Show', 2, 5, 'alice')

        self.assertTrue(result)
        self.assertEqual(captured['series_name'], 'Some Show')
        self.assertEqual(captured['season_number'], 2)
        self.assertEqual(captured['episode_number'], 5)
        self.assertEqual(captured['user_name'], 'alice')

    def test_unloaded_integration_returns_false(self):
        fake_integrations_module = types.ModuleType('integrations')
        fake_integrations_module.get_integration = lambda name: None
        with patch.dict(sys.modules, {'integrations': fake_integrations_module}):
            result = reconcile.replay_watch_event('plex', 'Some Show', 2, 5, 'alice')
        self.assertFalse(result)


class FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class CheckDelayTaggedSeriesTestCase(unittest.TestCase):
    """episeerr_utils.http/SONARR_URL/get_sonarr_headers/get_tag_mapping/
    get_or_create_rule_tag_id/resolve_rule_from_tags/
    cancel_queued_downloads_for_series/apply_initial_rule_selection are all
    patched on the real episeerr_utils module - check_delay_tagged_series()
    imports them locally at call time, so patching the module attribute is
    enough without needing a fake module in sys.modules."""

    def setUp(self):
        self._orig_media_processor = sys.modules.get('media_processor')

    def tearDown(self):
        if self._orig_media_processor is not None:
            sys.modules['media_processor'] = self._orig_media_processor
        else:
            sys.modules.pop('media_processor', None)

    def _install_settings(self, **overrides):
        settings = {'automation_held': False}
        settings.update(overrides)
        sys.modules['media_processor'] = _fake_media_processor(settings, config={'rules': {}})

    def test_held_automation_is_a_noop(self):
        self._install_settings(automation_held=True)
        with patch.object(episeerr_utils, 'get_or_create_rule_tag_id') as mock_tag:
            summary = reconcile.check_delay_tagged_series()
        self.assertFalse(summary['ran'])
        mock_tag.assert_not_called()

    def test_no_delay_tag_configured_is_a_noop(self):
        self._install_settings()
        with patch.object(episeerr_utils, 'get_or_create_rule_tag_id', return_value=None):
            summary = reconcile.check_delay_tagged_series()
        self.assertTrue(summary['ran'])
        self.assertEqual(summary['processed'], 0)

    def test_series_without_delay_tag_is_ignored(self):
        self._install_settings()
        with patch.object(episeerr_utils, 'get_or_create_rule_tag_id', return_value=99), \
             patch.object(episeerr_utils, 'get_sonarr_headers', return_value={}), \
             patch.object(episeerr_utils, 'get_tag_mapping', return_value={}), \
             patch.object(episeerr_utils.http, 'get', return_value=FakeResponse(
                 [{'id': 1, 'title': 'Show', 'tags': [1, 2]}])), \
             patch.object(episeerr_utils, 'apply_initial_rule_selection') as mock_apply:
            summary = reconcile.check_delay_tagged_series()
        self.assertEqual(summary['processed'], 0)
        mock_apply.assert_not_called()

    def test_select_tagged_series_is_left_alone(self):
        self._install_settings()
        with patch.object(episeerr_utils, 'get_or_create_rule_tag_id', return_value=99), \
             patch.object(episeerr_utils, 'get_sonarr_headers', return_value={}), \
             patch.object(episeerr_utils, 'get_tag_mapping', return_value={}), \
             patch.object(episeerr_utils.http, 'get', return_value=FakeResponse(
                 [{'id': 1, 'title': 'Show', 'tags': [99]}])), \
             patch.object(episeerr_utils, 'resolve_rule_from_tags', return_value=(None, True)), \
             patch.object(episeerr_utils, 'apply_initial_rule_selection') as mock_apply, \
             patch.object(episeerr_utils, 'cancel_queued_downloads_for_series') as mock_cancel:
            summary = reconcile.check_delay_tagged_series()
        self.assertEqual(summary['processed'], 0)
        mock_apply.assert_not_called()
        mock_cancel.assert_not_called()

    def test_unresolvable_rule_is_left_alone(self):
        self._install_settings()
        with patch.object(episeerr_utils, 'get_or_create_rule_tag_id', return_value=99), \
             patch.object(episeerr_utils, 'get_sonarr_headers', return_value={}), \
             patch.object(episeerr_utils, 'get_tag_mapping', return_value={}), \
             patch.object(episeerr_utils.http, 'get', return_value=FakeResponse(
                 [{'id': 1, 'title': 'Show', 'tags': [99]}])), \
             patch.object(episeerr_utils, 'resolve_rule_from_tags', return_value=(None, False)), \
             patch.object(episeerr_utils, 'apply_initial_rule_selection') as mock_apply:
            summary = reconcile.check_delay_tagged_series()
        self.assertEqual(summary['processed'], 0)
        mock_apply.assert_not_called()

    def test_resolvable_delay_tagged_series_cancels_queue_then_applies_rule(self):
        self._install_settings()
        calls = []
        with patch.object(episeerr_utils, 'get_or_create_rule_tag_id', return_value=99), \
             patch.object(episeerr_utils, 'get_sonarr_headers', return_value={}), \
             patch.object(episeerr_utils, 'get_tag_mapping', return_value={}), \
             patch.object(episeerr_utils.http, 'get', return_value=FakeResponse(
                 [{'id': 42, 'title': 'Show', 'tags': [99]}])), \
             patch.object(episeerr_utils, 'resolve_rule_from_tags', return_value=('one_at_a_time', False)), \
             patch.object(episeerr_utils, 'cancel_queued_downloads_for_series',
                          side_effect=lambda sid: calls.append(('cancel', sid))), \
             patch.object(episeerr_utils, 'apply_initial_rule_selection',
                          side_effect=lambda *a, **k: calls.append(('apply', a[0], a[2])) or True):
            summary = reconcile.check_delay_tagged_series()
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(calls, [('cancel', 42), ('apply', 42, 'one_at_a_time')])

    def test_one_series_erroring_does_not_stop_the_others(self):
        self._install_settings()
        processed_ids = []
        with patch.object(episeerr_utils, 'get_or_create_rule_tag_id', return_value=99), \
             patch.object(episeerr_utils, 'get_sonarr_headers', return_value={}), \
             patch.object(episeerr_utils, 'get_tag_mapping', return_value={}), \
             patch.object(episeerr_utils.http, 'get', return_value=FakeResponse([
                 {'id': 1, 'title': 'Broken Show', 'tags': [99]},
                 {'id': 2, 'title': 'Fine Show', 'tags': [99]},
             ])), \
             patch.object(episeerr_utils, 'resolve_rule_from_tags', return_value=('one_at_a_time', False)), \
             patch.object(episeerr_utils, 'cancel_queued_downloads_for_series',
                          side_effect=lambda sid: (_ for _ in ()).throw(RuntimeError('boom')) if sid == 1 else None), \
             patch.object(episeerr_utils, 'apply_initial_rule_selection',
                          side_effect=lambda *a, **k: processed_ids.append(a[0]) or True):
            summary = reconcile.check_delay_tagged_series()
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(processed_ids, [2])
        self.assertTrue(summary['errors'])


if __name__ == '__main__':
    unittest.main()
