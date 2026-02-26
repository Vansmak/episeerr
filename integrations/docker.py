# integrations/docker.py
"""
Docker Integration for Episeerr
Provides: Media stack container status and controls in the sidebar

No extra dependencies — uses requests (already installed) to talk
to the Docker socket or TCP host directly via HTTP.
"""

import logging
import requests
import requests.adapters
import socket
import urllib.parse
from typing import Dict, Any, Optional, List, Tuple
from flask import Blueprint, jsonify
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

# Default container name fragments used when no filter or stack is configured
DEFAULT_MEDIA_NAMES = [
    'sonarr', 'radarr', 'plex', 'jellyfin', 'emby',
    'qbittorrent', 'transmission', 'nzbget', 'sabnzbd',
    'prowlarr', 'jackett', 'bazarr', 'overseerr', 'requestrr',
    'tautulli', 'recyclarr', 'unpackerr',
]


# ---------------------------------------------------------------------------
# Unix socket adapter for requests
# ---------------------------------------------------------------------------

class UnixSocketAdapter(requests.adapters.HTTPAdapter):
    """Lets requests talk to a Unix domain socket."""

    def __init__(self, socket_path: str, **kwargs):
        self.socket_path = socket_path
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        # Build a session that connects via the Unix socket
        import http.client

        class UnixHTTPConnection(http.client.HTTPConnection):
            def __init__(self, socket_path):
                super().__init__('localhost')
                self._socket_path = socket_path

            def connect(self):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self._socket_path)
                self.sock = s

        parsed = urllib.parse.urlparse(request.url)
        path = parsed.path
        if parsed.query:
            path += '?' + parsed.query

        conn = UnixHTTPConnection(self.socket_path)
        conn.request(
            request.method,
            path,
            body=request.body,
            headers=dict(request.headers)
        )
        raw = conn.getresponse()

        # Build a requests.Response from the raw response
        response = requests.models.Response()
        response.status_code = raw.status
        response.headers = dict(raw.getheaders())
        response._content = raw.read()
        response.encoding = 'utf-8'
        return response


# ---------------------------------------------------------------------------
# Docker HTTP client — works with Unix socket or TCP
# ---------------------------------------------------------------------------

def _docker_get(host: str, path: str, params: dict = None) -> dict:
    """
    GET from Docker API. Returns parsed JSON or raises on error.
    host: unix:///var/run/docker.sock  OR  tcp://192.168.1.10:2375
    """
    if host.startswith('unix://'):
        socket_path = host[len('unix://'):]
        session = requests.Session()
        session.mount('http+unix://', UnixSocketAdapter(socket_path))
        url = f'http+unix://localhost{path}'
        if params:
            url += '?' + urllib.parse.urlencode(params)
        resp = session.get(url, timeout=10)
    else:
        # TCP — strip tcp:// prefix, use plain http
        base = host.replace('tcp://', 'http://')
        resp = requests.get(f'{base}{path}', params=params, timeout=10)

    resp.raise_for_status()
    return resp.json()


def _docker_post(host: str, path: str) -> int:
    """POST to Docker API (start/stop/restart). Returns status code."""
    if host.startswith('unix://'):
        socket_path = host[len('unix://'):]
        session = requests.Session()
        session.mount('http+unix://', UnixSocketAdapter(socket_path))
        url = f'http+unix://localhost{path}'
        resp = session.post(url, timeout=15)
    else:
        base = host.replace('tcp://', 'http://')
        resp = requests.post(f'{base}{path}', timeout=15)

    return resp.status_code


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------

class DockerIntegration(ServiceIntegration):

    @property
    def service_name(self) -> str:
        return 'docker'

    @property
    def display_name(self) -> str:
        return 'Docker'

    @property
    def description(self) -> str:
        return 'Media stack container status and controls in the sidebar'

    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/docker.png'

    @property
    def category(self) -> str:
        return 'utility'

    @property
    def default_port(self) -> int:
        return 2375

    # -----------------------------------------------------------------------
    # Custom setup fields
    # -----------------------------------------------------------------------

    def get_setup_fields(self) -> Optional[List[Dict]]:
        return [
            {
                'name': 'url',
                'label': 'Docker Host',
                'type': 'text',
                'placeholder': 'unix:///var/run/docker.sock',
                'required': True,
                'help_text': (
                    'Unix socket (unix:///var/run/docker.sock) or TCP '
                    '(tcp://192.168.1.10:2375). '
                    'For socket, mount it in your compose: '
                    '/var/run/docker.sock:/var/run/docker.sock:ro'
                )
            },
            {
                'name': 'api_key',
                'label': 'Compose Stack Name (optional)',
                'type': 'text',
                'placeholder': 'mediastack',
                'required': False,
                'help_text': (
                    'Show all containers belonging to a specific Docker Compose stack. '
                    'This is the project name from your compose file '
                    '(usually the folder name or set via COMPOSE_PROJECT_NAME). '
                    'Takes priority over Container Filter if both are set.'
                )
            },
            {
                'name': 'container_filter',
                'label': 'Container Filter (optional)',
                'type': 'text',
                'placeholder': 'sonarr,radarr,plex',
                'required': False,
                'help_text': (
                    'Comma-separated container name fragments to show. '
                    'Used only if Compose Stack Name is blank. '
                    'Leave both blank to use defaults (sonarr, radarr, plex, etc.)'
                )
            }
        ]

    # -----------------------------------------------------------------------
    # Connection test
    # -----------------------------------------------------------------------

    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        host = url or 'unix:///var/run/docker.sock'
        try:
            info = _docker_get(host, '/info')
            containers = _docker_get(host, '/containers/json', {'all': 'true'})
            version = info.get('ServerVersion', 'unknown')
            return True, f'Connected to Docker {version} — {len(containers)} containers found'
        except Exception as e:
            return False, f'Connection failed: {str(e)}'

    # -----------------------------------------------------------------------
    # Dashboard stats + widget
    # -----------------------------------------------------------------------

    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        host = url or 'unix:///var/run/docker.sock'
        try:
            containers = _docker_get(host, '/containers/json', {'all': 'true'})
            stack, filter_str = self._get_filter_config()
            media = self._filter_containers(containers, stack, filter_str)
            running = sum(1 for c in media if c.get('State') == 'running')
            return {'configured': True, 'running': running, 'total': len(media)}
        except Exception as e:
            return {'configured': True, 'error': str(e)}

    def get_dashboard_widget(self) -> Dict[str, Any]:
        return {
            'enabled': True,
            'pill': {
                'icon': 'fas fa-cubes',
                'icon_color': 'text-info',
                'template': '{running}/{total}',
                'fields': ['running', 'total']
            }
        }

    # -----------------------------------------------------------------------
    # Blueprint — sidebar API routes
    # -----------------------------------------------------------------------

    def create_blueprint(self) -> Optional[Blueprint]:
        bp = Blueprint('docker_integration', __name__)
        integration = self

        @bp.route('/api/docker/media-containers')
        def media_containers():
            host, stack, filter_str = integration._get_full_config()
            if not host:
                return jsonify({
                    'available': False,
                    'containers': [],
                    'error': 'Docker not configured',
                    'setup_url': '/setup'
                })

            try:
                raw = _docker_get(host, '/containers/json', {'all': 'true'})
                media = integration._filter_containers(raw, stack, filter_str)

                result = []
                for c in media:
                    name = (c.get('Names') or ['unknown'])[0].lstrip('/')
                    result.append({
                        'id': c.get('Id', '')[:12],
                        'name': name,
                        'status': c.get('State', 'unknown'),   # running / exited / paused
                        'image': c.get('Image', ''),
                    })

                # Running first, then alpha
                result.sort(key=lambda x: (0 if x['status'] == 'running' else 1, x['name']))
                return jsonify({'available': True, 'containers': result})

            except Exception as e:
                logger.error(f'Docker list error: {e}')
                return jsonify({'available': False, 'containers': [], 'error': str(e)})

        @bp.route('/api/docker/container/<container_id>/<action>', methods=['POST'])
        def container_action(container_id, action):
            if action not in ('start', 'stop', 'restart'):
                return jsonify({'success': False, 'error': f'Invalid action: {action}'}), 400

            host, _, _ = integration._get_full_config()
            if not host:
                return jsonify({'success': False, 'error': 'Docker not configured'}), 500

            try:
                status = _docker_post(host, f'/containers/{container_id}/{action}')
                # Docker returns 204 (no content) on success, 304 for no-op (already in that state)
                if status in (204, 304):
                    return jsonify({'success': True, 'action': action})
                else:
                    return jsonify({'success': False, 'error': f'Docker returned {status}'}), 500
            except Exception as e:
                logger.error(f'Container {action} failed: {e}')
                return jsonify({'success': False, 'error': str(e)}), 500

        return bp

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _get_full_config(self) -> Tuple[str, str, str]:
        """Returns (host, stack_name, container_filter)"""
        try:
            from settings_db import get_service
            svc = get_service('docker') or {}
            host = svc.get('url') or 'unix:///var/run/docker.sock'
            config = svc.get('config') or {}
            stack = config.get('api_key', '').strip()          # Compose stack name
            filter_str = config.get('container_filter', '').strip()
            return host, stack, filter_str
        except Exception:
            return 'unix:///var/run/docker.sock', '', ''

    def _get_filter_config(self) -> Tuple[str, str]:
        """Returns (stack_name, container_filter) only"""
        _, stack, filter_str = self._get_full_config()
        return stack, filter_str

    def _filter_containers(self, containers: list, stack: str, filter_str: str) -> list:
        """
        Priority:
        1. Stack name — filter by com.docker.compose.project label
        2. Container filter — filter by name fragments
        3. Default media names list
        """
        if stack:
            return [
                c for c in containers
                if c.get('Labels', {}).get('com.docker.compose.project', '').lower() == stack.lower()
            ]

        if filter_str:
            fragments = [f.strip().lower() for f in filter_str.split(',') if f.strip()]
        else:
            fragments = DEFAULT_MEDIA_NAMES

        result = []
        for c in containers:
            names = [n.lstrip('/').lower() for n in (c.get('Names') or [])]
            if any(frag in name for frag in fragments for name in names):
                result.append(c)
        return result


# Export integration instance
integration = DockerIntegration()
