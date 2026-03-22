# integrations/sonos.py - FULLY SELF-CONTAINED
"""
Sonos Integration - Completely Self-Contained
No manual edits to episeerr.py or dashboard.html required!

Uses the Sonos local UPnP/SOAP API (no library required).
Discovers zones via /status/topology; falls back to polling the configured
speaker directly if topology is unavailable.

Setup:
  - URL: IP or hostname of any Sonos speaker (e.g. http://192.168.1.10)
  - API Key: leave blank (Sonos local API requires no auth)
"""

from integrations.base import ServiceIntegration
from typing import Dict, Any, Optional, Tuple
from flask import Blueprint, jsonify, request
import requests
import logging
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_TRANSPORT_PATH  = '/MediaRenderer/AVTransport/Control'
_TOPOLOGY_PATH   = '/ZoneGroupTopology/Control'
_DEVICE_PATH     = '/xml/device_description.xml'
_ZONE_PATH       = '/status/topology'          # deprecated in S2 firmware ≥ 14.x
_AVT_NS          = 'urn:schemas-upnp-org:service:AVTransport:1'
_ZGT_NS          = 'urn:schemas-upnp-org:service:ZoneGroupTopology:1'


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _base_url(raw_url: str) -> str:
    """Normalise any input → http://host:1400"""
    url = raw_url.strip().rstrip('/')
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc or url
    return f"http://{host}:1400"


def _soap(base: str, action: str, body_inner: str, timeout: int = 5) -> Optional[ET.Element]:
    """Fire a UPnP SOAP action at the AVTransport service."""
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        f'<u:{action} xmlns:u="{_AVT_NS}">'
        f'{body_inner}'
        f'</u:{action}>'
        '</s:Body>'
        '</s:Envelope>'
    )
    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': f'"{_AVT_NS}#{action}"',
    }
    try:
        resp = requests.post(f"{base}{_TRANSPORT_PATH}", data=envelope,
                             headers=headers, timeout=timeout)
        resp.raise_for_status()
        return ET.fromstring(resp.text)
    except Exception as e:
        logger.debug(f"SOAP {action} @ {base} failed: {e}")
        return None


def _friendly_name(base: str) -> str:
    """Return the UPnP friendly name for a speaker, or 'Unknown'."""
    try:
        resp = requests.get(f"{base}{_DEVICE_PATH}", timeout=5)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        el = root.find('.//{urn:schemas-upnp-org:device-1-0}friendlyName')
        return el.text if el is not None else 'Unknown'
    except Exception:
        return 'Unknown'


def _transport_info(base: str) -> Dict[str, Any]:
    """
    Poll a single speaker for its current transport + track state.
    Returns: {is_playing, track, artist, album, album_art_url}
    """
    result: Dict[str, Any] = {
        'is_playing':    False,
        'track':         None,
        'artist':        None,
        'album':         None,
        'album_art_url': None,
    }

    # Transport state (playing / paused / stopped)
    root = _soap(base, 'GetTransportInfo', '<InstanceID>0</InstanceID>')
    if root is not None:
        # Try namespaced first, then bare
        state = (root.findtext(f'.//{{{_AVT_NS}}}CurrentTransportState')
                 or root.findtext('.//CurrentTransportState'))
        result['is_playing'] = (state == 'PLAYING')

    # Track metadata
    root2 = _soap(base, 'GetPositionInfo', '<InstanceID>0</InstanceID>')
    if root2 is not None:
        meta_text = (root2.findtext(f'.//{{{_AVT_NS}}}TrackMetaData')
                     or root2.findtext('.//TrackMetaData'))
        if meta_text and meta_text.strip() not in ('', 'NOT_IMPLEMENTED'):
            try:
                meta = ET.fromstring(meta_text)
                dc   = 'http://purl.org/dc/elements/1.1/'
                upnp = 'urn:schemas-upnp-org:metadata-1-0/upnp/'

                result['track']  = meta.findtext(f'.//{{{dc}}}title')
                result['artist'] = meta.findtext(f'.//{{{dc}}}creator')
                result['album']  = meta.findtext(f'.//{{{upnp}}}album')

                art = meta.findtext(f'.//{{{upnp}}}albumArtURI') or ''
                art = art.strip()
                if art:
                    result['album_art_url'] = art if art.startswith('http') else f"{base}{art}"
            except ET.ParseError as pe:
                logger.debug(f"TrackMetaData parse error: {pe}")

    return result


def _parse_zone_groups(root: ET.Element, base: str) -> list:
    """
    Parse ZoneGroup elements from an ElementTree root into zone dicts.
    Works whether root IS the ZoneGroups element or contains it deeper.
    Returns an empty list if no valid groups are found.
    """
    zones = []
    for zg in root.findall('.//ZoneGroup'):
        coord_uid  = zg.get('Coordinator', '')
        members    = []
        coord_url  = None
        coord_name = None

        for zm in zg.findall('ZoneGroupMember'):
            uid      = zm.get('UUID', '')
            name     = zm.get('ZoneName', 'Unknown')
            location = zm.get('Location', '')

            if location:
                p  = urlparse(location)
                mb = f"http://{p.hostname}:1400"
            else:
                mb = base

            members.append({'name': name, 'url': mb, 'uid': uid})

            if uid == coord_uid:
                coord_url  = mb
                coord_name = name

        # If coordinator UID didn't match any member (stereo-pair satellite,
        # invisible sub-member, firmware quirk), use the first member instead
        # so the zone isn't silently dropped.
        if coord_url is None and members:
            coord_url  = members[0]['url']
            coord_name = members[0]['name']
            logger.warning(
                f"Sonos: coordinator UUID {coord_uid!r} not found in ZoneGroupMember list; "
                f"using first member ({coord_name!r}) as coordinator"
            )

        if coord_url:
            zones.append({
                'name':            coord_name or 'Unknown',
                'coordinator_url': coord_url,
                'members':         members,
            })

    return zones


def _get_zones(base: str) -> list:
    """
    Discover all Sonos zones, trying three methods in order:

    1. UPnP SOAP GetZoneGroupState (ZoneGroupTopology service) — works on all
       firmware including S2 ≥ 14.x where /status/topology is deprecated.
    2. HTTP GET /status/topology — older firmware fallback.
    3. Single-speaker synthetic zone — last resort.

    Returns list of {name, coordinator_url, members:[{name,url}], topology_source}.
    """
    # ── Method 1: UPnP ZoneGroupTopology SOAP ─────────────────────────────────
    try:
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            f'<u:GetZoneGroupState xmlns:u="{_ZGT_NS}"/>'
            '</s:Body>'
            '</s:Envelope>'
        )
        headers = {
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPACTION':   f'"{_ZGT_NS}#GetZoneGroupState"',
        }
        resp = requests.post(f"{base}{_TOPOLOGY_PATH}", data=envelope,
                             headers=headers, timeout=6)
        resp.raise_for_status()
        soap_root = ET.fromstring(resp.text)
        state_el  = soap_root.find('.//ZoneGroupState')
        if state_el is not None and state_el.text and state_el.text.strip():
            zgs_root = ET.fromstring(state_el.text)
            zones    = _parse_zone_groups(zgs_root, base)
            if zones:
                logger.debug(f"Sonos: discovered {len(zones)} zone(s) via ZoneGroupTopology SOAP")
                return [dict(z, topology_source='soap') for z in zones]
            logger.warning("Sonos: ZoneGroupTopology SOAP returned no ZoneGroup elements")
        else:
            logger.warning("Sonos: ZoneGroupTopology SOAP response had empty ZoneGroupState")
    except Exception as e:
        logger.warning(f"Sonos: ZoneGroupTopology SOAP failed ({e})")

    # ── Method 2: HTTP /status/topology (older firmware) ──────────────────────
    try:
        resp = requests.get(f"{base}{_ZONE_PATH}", timeout=6)
        resp.raise_for_status()
        root  = ET.fromstring(resp.text)
        zones = _parse_zone_groups(root, base)
        if zones:
            logger.debug(f"Sonos: discovered {len(zones)} zone(s) via /status/topology")
            return [dict(z, topology_source='topology') for z in zones]
        logger.warning("Sonos: /status/topology returned no ZoneGroup elements")
    except Exception as e:
        logger.warning(f"Sonos: /status/topology fetch failed ({e})")

    # ── Method 3: single-speaker fallback ─────────────────────────────────────
    logger.warning(f"Sonos: all topology methods failed; using single-speaker fallback for {base}")
    name = _friendly_name(base)
    return [{
        'name':            name,
        'coordinator_url': base,
        'members':         [{'name': name, 'url': base, 'uid': ''}],
        'topology_source': 'fallback',
    }]


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------

class SonosIntegration(ServiceIntegration):
    """Self-contained Sonos integration — now-playing widget + zone pills"""

    @property
    def service_name(self) -> str:
        return 'sonos'

    @property
    def display_name(self) -> str:
        return 'Sonos'

    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/sonos.png'

    @property
    def description(self) -> str:
        return 'Multi-room audio with now playing widget and zone status'

    @property
    def category(self) -> str:
        return 'dashboard'

    @property
    def default_port(self) -> int:
        return 1400

    # -----------------------------------------------------------------------

    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        try:
            base = _base_url(url)
            resp = requests.get(f"{base}{_DEVICE_PATH}", timeout=8)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                el = root.find('.//{urn:schemas-upnp-org:device-1-0}friendlyName')
                name = el.text if el is not None else 'Unknown'
                return True, f"Connected to {name}"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, f"Error: {e}"

    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        try:
            base  = _base_url(url)
            zones = _get_zones(base)

            zone_data    = []
            active_zone  = None

            for zone in zones:
                info = _transport_info(zone['coordinator_url'])
                z = {
                    'name':          zone['name'],
                    'is_playing':    info['is_playing'],
                    'track':         info.get('track'),
                    'artist':        info.get('artist'),
                    'album_art_url': info.get('album_art_url'),
                    'members':       [m['name'] for m in zone['members']],
                }
                zone_data.append(z)
                if info['is_playing'] and active_zone is None:
                    active_zone = z

            playing_count   = sum(1 for z in zone_data if z['is_playing'])
            topology_source = zones[0].get('topology_source', 'unknown') if zones else 'unknown'

            return {
                'configured':      True,
                'zones':           zone_data,
                'active_zone':     active_zone,
                'playing_count':   playing_count,
                'total_zones':     len(zone_data),
                'topology_source': topology_source,
            }

        except Exception as e:
            logger.error(f"Sonos stats error: {e}")
            return {'configured': True, 'error': str(e)}

    def get_dashboard_widget(self) -> Dict[str, Any]:
        return {
            'enabled': True,
            'pill': {
                'icon':       'fas fa-volume-up',
                'icon_color': 'text-warning',
                'template':   '{playing_count} playing / {total_zones} zones',
                'fields':     ['playing_count', 'total_zones'],
            },
            'has_custom_widget': True,
        }

    # -----------------------------------------------------------------------

    def create_blueprint(self) -> Blueprint:
        bp = Blueprint('sonos_integration', __name__)
        integration = self

        @bp.route('/api/integration/sonos/widget')
        def widget():
            try:
                from settings_db import get_service

                config = get_service('sonos', 'default')
                if not config:
                    return jsonify({'success': False, 'message': 'Not configured'})

                url = config.get('url', '')
                if not url:
                    return jsonify({'success': False, 'message': 'No URL configured'})

                zone_param = request.args.get('zone', None, type=int)

                stats = integration.get_dashboard_stats(url, config.get('api_key', ''))
                zones = stats.get('zones', [])

                # ── Error state stub ─────────────────────────────────────────
                if stats.get('error') and not zones:
                    html = f'''<div style="background:rgba(255,255,255,0.04);border-radius:6px;padding:8px 10px;">
                        <div class="d-flex align-items-center gap-2 mb-1">
                            <img src="{integration.icon}" style="width:12px;height:12px;" onerror="this.style.display='none'">
                            <span class="text-muted" style="font-size:9px;text-transform:uppercase;letter-spacing:.06em;">Sonos</span>
                        </div>
                        <div class="text-muted" style="font-size:11px;">Unavailable — {stats["error"]}</div>
                    </div>'''
                    return jsonify({'success': True, 'html': html})

                # ── Pick which zone to display ────────────────────────────────
                if zone_param is not None and 0 <= zone_param < len(zones):
                    selected_idx = zone_param
                else:
                    # Auto: first playing zone, else first zone
                    selected_idx = 0
                    for i, z in enumerate(zones):
                        if z['is_playing']:
                            selected_idx = i
                            break

                display_zone = zones[selected_idx] if zones else None

                # ── Now-playing panel ─────────────────────────────────────────
                if display_zone and display_zone.get('is_playing'):
                    track  = display_zone.get('track')  or 'Unknown track'
                    artist = display_zone.get('artist') or ''
                    art    = display_zone.get('album_art_url')

                    if art:
                        art_html = (
                            f'<img src="{art}" class="rounded" '
                            f'style="width:64px;height:64px;object-fit:cover;flex-shrink:0;" '
                            f"onerror=\"this.style.display='none'\">"
                        )
                    else:
                        art_html = (
                            '<div class="rounded d-flex align-items-center justify-content-center flex-shrink-0" '
                            'style="width:64px;height:64px;background:rgba(255,165,0,0.12);">'
                            '<i class="fas fa-volume-up text-warning" style="font-size:22px;"></i></div>'
                        )

                    now_playing_html = f'''
                    <div class="d-flex align-items-start gap-2">
                        {art_html}
                        <div class="flex-grow-1 overflow-hidden">
                            <div class="text-truncate fw-semibold" style="font-size:12px;line-height:1.3;">{track}</div>
                            <div class="text-truncate text-muted" style="font-size:11px;line-height:1.3;">{artist}</div>
                            <span class="badge bg-warning text-dark mt-1" style="font-size:9px;">
                                <i class="fas fa-play me-1"></i>Playing
                            </span>
                        </div>
                    </div>'''

                elif display_zone:
                    zone_name = display_zone.get('name', 'Unknown')
                    now_playing_html = f'''
                    <div class="d-flex align-items-center gap-2">
                        <div class="rounded d-flex align-items-center justify-content-center flex-shrink-0"
                             style="width:64px;height:64px;background:rgba(255,255,255,0.04);">
                            <i class="fas fa-volume-mute text-muted" style="font-size:22px;opacity:0.4;"></i>
                        </div>
                        <div>
                            <div class="text-muted" style="font-size:12px;">{zone_name}</div>
                            <div class="text-muted" style="font-size:11px;opacity:0.6;">Idle</div>
                        </div>
                    </div>'''
                else:
                    now_playing_html = '<div class="text-muted small">No zones found</div>'

                # ── Zone pills ────────────────────────────────────────────────
                zone_pills = []
                for i, z in enumerate(zones):
                    members  = z.get('members', [z['name']])
                    is_selected = (i == selected_idx)

                    if z['is_playing']:
                        label = f"{z['name']} +{len(members)-1}" if len(members) > 1 else z['name']
                        tip   = ', '.join(members)
                        if is_selected:
                            cls = 'bg-warning text-dark border border-warning'
                        else:
                            cls = 'bg-warning bg-opacity-25 text-warning border border-warning border-opacity-50'
                        icon = 'fas fa-volume-up'
                    else:
                        label = z['name']
                        tip   = f"{z['name']} (idle)"
                        if is_selected:
                            cls = 'bg-secondary text-white border border-secondary'
                        else:
                            cls = 'bg-secondary bg-opacity-10 text-muted border border-secondary border-opacity-25'
                        icon = 'fas fa-volume-mute'

                    zone_pills.append(
                        f'<span class="badge rounded-pill px-2 py-1 {cls}" '
                        f'style="font-size:10px;cursor:pointer;" title="{tip}" '
                        f'onclick="sonosSelectZone({i})">'
                        f'<i class="{icon} me-1"></i>{label}</span>'
                    )

                pills_html = (
                    '<div class="d-flex flex-wrap gap-1 mt-2">' + ''.join(zone_pills) + '</div>'
                ) if zone_pills else ''

                # ── Summary line ──────────────────────────────────────────────
                playing_count   = sum(1 for z in zones if z['is_playing'])
                topo_src        = stats.get('topology_source', 'unknown')
                fallback_warn   = ' ⚠ fallback' if topo_src == 'fallback' else ''
                summary = f'{playing_count} / {len(zones)} playing{fallback_warn}'

                # ── Assemble ──────────────────────────────────────────────────
                html = f'''<div style="background:rgba(255,255,255,0.04);border-radius:6px;padding:10px 12px;">
                    <div class="d-flex align-items-center gap-2 mb-2">
                        <img src="{integration.icon}" style="width:12px;height:12px;flex-shrink:0;"
                             onerror="this.style.display='none'">
                        <span class="text-muted" style="font-size:9px;text-transform:uppercase;letter-spacing:.08em;">Sonos</span>
                        <span class="ms-auto text-muted" style="font-size:9px;">{summary}</span>
                    </div>
                    {now_playing_html}
                    {pills_html}
                </div>'''

                return jsonify({'success': True, 'html': html})

            except Exception as e:
                logger.error(f"Sonos widget error: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @bp.route('/api/integration/sonos/debug')
        def debug():
            """Diagnostic endpoint — hit this from the browser to see what's happening."""
            try:
                from settings_db import get_service

                config = get_service('sonos', 'default')
                if not config:
                    return jsonify({'error': 'Not configured in DB'})

                url  = config.get('url', '')
                base = _base_url(url)

                # Device description
                dev_ok   = False
                dev_name = None
                try:
                    r = requests.get(f"{base}{_DEVICE_PATH}", timeout=5)
                    dev_ok = r.status_code == 200
                    if dev_ok:
                        root = ET.fromstring(r.text)
                        el = root.find('.//{urn:schemas-upnp-org:device-1-0}friendlyName')
                        dev_name = el.text if el is not None else 'parse error'
                except Exception as e:
                    dev_name = str(e)

                # ZoneGroupTopology SOAP (primary method, S2 firmware)
                zgt_ok    = False
                zgt_zones = None
                zgt_text  = None
                try:
                    envelope = (
                        '<?xml version="1.0" encoding="utf-8"?>'
                        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                        '<s:Body>'
                        f'<u:GetZoneGroupState xmlns:u="{_ZGT_NS}"/>'
                        '</s:Body>'
                        '</s:Envelope>'
                    )
                    r = requests.post(f"{base}{_TOPOLOGY_PATH}", data=envelope,
                                      headers={'Content-Type': 'text/xml; charset="utf-8"',
                                               'SOAPACTION': f'"{_ZGT_NS}#GetZoneGroupState"'},
                                      timeout=5)
                    zgt_ok = r.status_code == 200
                    if zgt_ok:
                        sr   = ET.fromstring(r.text)
                        sel  = sr.find('.//ZoneGroupState')
                        zgt_text = (sel.text or '')[:500] if sel is not None else '(no ZoneGroupState element)'
                        if sel is not None and sel.text:
                            zg_root   = ET.fromstring(sel.text)
                            zgt_zones = len(zg_root.findall('.//ZoneGroup'))
                    else:
                        zgt_text = f"HTTP {r.status_code}"
                except Exception as e:
                    zgt_text = str(e)

                # HTTP /status/topology (legacy method, older firmware)
                topo_ok   = False
                topo_text = None
                try:
                    r = requests.get(f"{base}{_ZONE_PATH}", timeout=5)
                    topo_ok   = r.status_code == 200
                    topo_text = r.text[:300] if topo_ok else f"HTTP {r.status_code}"
                except Exception as e:
                    topo_text = str(e)

                # Transport SOAP
                soap_ok    = False
                soap_state = None
                root = _soap(base, 'GetTransportInfo', '<InstanceID>0</InstanceID>')
                if root is not None:
                    soap_ok    = True
                    soap_state = (root.findtext(f'.//{{{_AVT_NS}}}CurrentTransportState')
                                  or root.findtext('.//CurrentTransportState'))

                # Full stats
                stats = integration.get_dashboard_stats(url, '')

                return jsonify({
                    'base_url':            base,
                    'device_ok':           dev_ok,
                    'device_name':         dev_name,
                    'zgt_soap_ok':         zgt_ok,
                    'zgt_zone_count':      zgt_zones,
                    'zgt_state_text':      zgt_text,
                    'legacy_topology_ok':  topo_ok,
                    'legacy_topology_text': topo_text,
                    'transport_soap_ok':   soap_ok,
                    'transport_state':     soap_state,
                    'stats':               stats,
                })

            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @bp.route('/api/integration/sonos/zones')
        def zones():
            """Raw zone/playback JSON."""
            try:
                from settings_db import get_service
                config = get_service('sonos', 'default')
                if not config:
                    return jsonify({'error': 'Not configured'}), 404
                stats = integration.get_dashboard_stats(config.get('url', ''), '')
                return jsonify(stats)
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        return bp

    def get_setup_fields(self) -> list:
        return [
            {
                'name':        'url',
                'label':       'Sonos Speaker IP / URL',
                'type':        'text',
                'placeholder': 'http://192.168.1.10',
                'help_text':   'IP or hostname of any one Sonos speaker — zones are discovered automatically',
            },
            {
                'name':        'api_key',
                'label':       'API Key (not required)',
                'type':        'text',
                'placeholder': '',
                'help_text':   'Sonos local API needs no authentication — leave blank',
            },
        ]


# Export integration instance
integration = SonosIntegration()
