"""
DockerIntegration._get_full_config() must not guess a default socket path
when the Docker integration has never been configured (episeerr#116).

Previously it always fell back to 'unix:///var/run/docker.sock', so every
page load (base.html calls /api/docker/media-containers unconditionally)
tried to hit a socket that was never mounted and logged an ERROR, even on
installs that never touched the Docker integration at all.

    python3 -m unittest tests.test_docker_integration_unconfigured -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix='episeerr_docker_test_')
os.environ.setdefault('SETTINGS_DB_PATH', os.path.join(_TMP, 'settings.db'))

import settings_db  # noqa: E402
from integrations.docker import DockerIntegration  # noqa: E402


class DockerUnconfiguredTestCase(unittest.TestCase):
    def setUp(self):
        self.integration = DockerIntegration()

    def test_unconfigured_returns_empty_host(self):
        with mock.patch.object(settings_db, 'get_service', return_value=None):
            host, stack, filter_str = self.integration._get_full_config()
        self.assertEqual(host, '')

    def test_configured_with_no_host_field_falls_back_to_default_socket(self):
        with mock.patch.object(settings_db, 'get_service',
                                return_value={'config': {}}):
            host, _, _ = self.integration._get_full_config()
        self.assertEqual(host, 'unix:///var/run/docker.sock')

    def test_configured_with_explicit_host_is_used(self):
        with mock.patch.object(settings_db, 'get_service',
                                return_value={'config': {'docker_host': 'tcp://1.2.3.4:2375'}}):
            host, _, _ = self.integration._get_full_config()
        self.assertEqual(host, 'tcp://1.2.3.4:2375')

    def test_lookup_exception_returns_empty_host(self):
        with mock.patch.object(settings_db, 'get_service',
                                side_effect=RuntimeError('db locked')):
            host, stack, filter_str = self.integration._get_full_config()
        self.assertEqual(host, '')


if __name__ == '__main__':
    unittest.main()
