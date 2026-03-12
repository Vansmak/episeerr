"""
Dispatcharr Integration for Episeerr
─────────────────────────────────────
Provides:   Live IPTV stream monitoring — active channels, viewers, bitrate
Auth:       X-Api-Key header (set via Profile → API Key in Dispatcharr)
Widget:     Dashboard pill showing active stream count + per-stream detail rows
Webhooks:   Two events only — channel_start (or channel_started) and
            channel_stop (or channel_stopped).  Episeerr adds the stream
            immediately on start and removes it immediately on stop, so the
            widget always reflects exactly what is playing right now.
            A background API sync fires after each start to fill in details
            (bitrate, resolution, clients).  Falls back to a single API sync
            on widget load if webhooks are not configured.

Endpoints registered:
    POST /api/integration/dispatcharr/webhook   ← Dispatcharr posts events here
    GET  /api/integration/dispatcharr/widget    ← Dashboard fetches widget HTML
    GET  /api/integration/dispatcharr/status    ← Debug: raw state as JSON
    POST /api/integration/dispatcharr/sync      ← Force a full API re-sync

Dispatcharr setup:
    Connect → Integrations → Add Webhook
    URL: http://<episeerr>:5002/api/integration/dispatcharr/webhook
    Triggers: Channel Started, Channel Stopped  (only these two are needed)
"""

import re
import time
import threading
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  Module-level stream state  (webhook-driven, shared across requests)
# ══════════════════════════════════════════════════════════════════

_lock = threading.Lock()
_active_streams: Dict[str, Dict] = {}   # keyed by channel_id (UUID string)
_last_sync:      Optional[datetime] = None


# ══════════════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════════════

def _parse_ua(ua: str) -> str:
    """'TiviMate/5.2.0 (Android 11)' → 'TiviMate · Android'"""
    if not ua:
        return "Unknown"
    m = re.match(r'^([^/\s]+)(?:/[\d.]+)?\s*(?:\(([^)]+)\))?', ua)
    if m:
        app  = m.group(1).strip()
        plat = (m.group(2) or "").split(";")[0].strip()
        plat = re.sub(r"\s+[\d.]+$", "", plat).strip()
        return f"{app} · {plat}" if plat else app
    return ua[:40]


def _fmt_uptime(seconds) -> str:
    """Seconds → 'h:mm:ss' or 'm:ss'"""
    s = int(float(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _normalize_channel(ch: dict, now: Optional[datetime] = None) -> dict:
    """Build a uniform stream dict from a /proxy/ts/status channel object."""
    if now is None:
        now = datetime.now(timezone.utc)
    clients = [
        {
            "id":    c.get("client_id", ""),
            "label": _parse_ua(c.get("user_agent", "")),
            "ip":    c.get("ip_address", ""),
            "since": c.get("connected_since", 0),
        }
        for c in ch.get("clients", [])
    ]
    return {
        "channel_id":   ch.get("channel_id", ""),
        "channel_name": ch.get("stream_name", "Unknown"),
        "state":        ch.get("state", "active"),   # active | error | reconnecting
        "clients":      clients,
        "client_count": ch.get("client_count", len(clients)),
        "uptime":       ch.get("uptime", 0),
        "avg_bitrate":  ch.get("avg_bitrate", ""),
        "resolution":   ch.get("resolution", ""),
        "video_codec":  ch.get("video_codec", ""),
        "source_fps":   ch.get("source_fps", 0),
        "recording":    False,
        "failover":     False,
        "started_at":   now,
        "last_seen":    now,   # updated on every API sync — used for staleness detection
    }


def _api_sync(url: str, api_key: str) -> bool:
    """Replace _active_streams with live data from /proxy/ts/status."""
    global _last_sync
    try:
        resp = requests.get(
            f"{url.rstrip('/')}/proxy/ts/status",
            headers={"X-Api-Key": api_key},
            timeout=5,
        )
        if not resp.ok:
            logger.warning(f"[Dispatcharr] status API returned {resp.status_code}")
            return False

        now = datetime.now(timezone.utc)
        with _lock:
            # Build fresh state from API, but preserve started_at timestamps
            # recorded when the webhook fired so live uptime stays accurate.
            existing_starts = {
                cid: s["started_at"]
                for cid, s in _active_streams.items()
                if s.get("started_at")
            }
            _active_streams.clear()
            for ch in resp.json().get("channels", []):
                cid  = ch.get("channel_id", "")
                norm = _normalize_channel(ch, now)
                if cid in existing_starts:
                    norm["started_at"] = existing_starts[cid]
                _active_streams[cid] = norm
            _last_sync = now
        return True

    except Exception as exc:
        logger.warning(f"[Dispatcharr] API sync failed: {exc}")
        return False


def _bg_sync(url: str, api_key: str, delay: float = 1.0):
    """Fire-and-forget: wait `delay` seconds then sync from the API."""
    def _run():
        time.sleep(delay)
        _api_sync(url, api_key)
    threading.Thread(target=_run, daemon=True, name="dispatcharr-sync").start()


def _get_saved_config() -> Optional[Dict]:
    """Load dispatcharr config from settings_db (url, api_key, extras)."""
    try:
        from settings_db import get_service
        svc = get_service("dispatcharr")
        if not svc:
            return None
        return {
            "url":     svc.get("url", ""),
            "api_key": svc.get("api_key", ""),
            **svc.get("config", {}),   # includes callback_url, webhook_integration_id, etc.
        }
    except Exception as exc:
        logger.warning(f"[Dispatcharr] config load failed: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════
#  Widget HTML renderer
# ══════════════════════════════════════════════════════════════════

def _render_widget() -> str:
    with _lock:
        streams = list(_active_streams.values())

    if not streams:
        return (
            '<div style="padding:10px 4px;">'
            '  <div class="d-flex align-items-center gap-2 text-muted" style="font-size:0.82rem;">'
            '    <i class="fas fa-satellite-dish" style="font-size:0.9rem;opacity:0.35;"></i>'
            '    <span style="opacity:0.55;">No active streams</span>'
            '  </div>'
            '</div>'
        )

    now_utc = datetime.now(timezone.utc)
    rows = []
    for s in streams:
        name    = s.get("channel_name", "Unknown")
        state   = s.get("state", "active")
        started = s.get("started_at")
        if started:
            uptime = _fmt_uptime((now_utc - started).total_seconds())
        else:
            uptime = _fmt_uptime(s.get("uptime", 0))
        bitrate = s.get("avg_bitrate", "")
        res     = s.get("resolution", "")
        clients = s.get("clients", [])
        count   = s.get("client_count", len(clients))
        rec     = s.get("recording", False)
        failover = s.get("failover", False)

        # Client label
        if count == 0:
            client_label = ""
        elif count == 1 and clients:
            client_label = clients[0].get("label", "")
        else:
            client_label = f"{count} viewers"

        # State indicator dot
        if state == "error":
            dot = '<span style="color:#dc3545;font-size:0.75rem;">⚠</span>'
        elif state == "reconnecting":
            dot = '<span style="color:#ffc107;font-size:0.5rem;">●</span>'
        else:
            dot = '<span style="color:#28a745;font-size:0.5rem;">●</span>'

        # Optional badges
        badges = ""
        if rec:
            badges += '<span class="badge ms-1" style="background:#dc3545;font-size:0.6rem;padding:2px 4px;">REC</span>'
        if failover:
            badges += '<span class="badge ms-1" style="background:#fd7e14;font-size:0.6rem;padding:2px 4px;">FAILOVER</span>'

        res_badge = (
            f'<span class="badge bg-secondary ms-1" style="font-size:0.6rem;padding:2px 5px;">{res}</span>'
            if res else ""
        )
        bitrate_span = (
            f'<span class="text-muted ms-1" style="font-size:0.7rem;white-space:nowrap;">{bitrate}</span>'
            if bitrate else ""
        )
        client_span = (
            f'<span class="text-muted text-truncate" style="font-size:0.72rem;max-width:140px;flex-shrink:1;">'
            f'{client_label}</span>'
            if client_label else ""
        )

        rows.append(
            f'<div class="d-flex align-items-center gap-2 py-1 px-2 mb-1 rounded"'
            f'     style="background:rgba(255,255,255,0.04);">'
            f'  {dot}'
            f'  <span class="fw-medium text-truncate" style="flex:1;font-size:0.82rem;">{name}</span>'
            f'  {badges}'
            f'  {client_span}'
            f'  {res_badge}'
            f'  {bitrate_span}'
            f'  <span class="text-muted ms-2"'
            f'        style="font-size:0.7rem;font-variant-numeric:tabular-nums;white-space:nowrap;">'
            f'    {uptime}</span>'
            f'</div>'
        )

    return f'<div style="padding:4px 0;">{"".join(rows)}</div>'


# ══════════════════════════════════════════════════════════════════
#  Integration class
# ══════════════════════════════════════════════════════════════════

class DispatcharrIntegration(ServiceIntegration):

    # ── Metadata ──────────────────────────────────────────────────

    @property
    def service_name(self) -> str:
        return "dispatcharr"

    @property
    def display_name(self) -> str:
        return "Dispatcharr"

    @property
    def description(self) -> str:
        return "Live IPTV stream monitoring — active channels, viewers, bitrate"

    @property
    def icon(self) -> str:
        # Dispatcharr serves its own logo; update to your instance URL if desired
        # e.g. 'http://192.168.1.x:9191/logo.png'
        return "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/dispatcharr.png"

    @property
    def category(self) -> str:
        return "dashboard"

    @property
    def default_port(self) -> int:
        return 9191

    # ── Setup fields ──────────────────────────────────────────────

    def get_setup_fields(self) -> Optional[List[Dict]]:
        return [
            {
                "name":        "url",
                "label":       "Dispatcharr URL",
                "type":        "text",
                "placeholder": "http://192.168.1.100:9191",
                "required":    True,
                "help_text":   "Base URL of your Dispatcharr instance",
            },
            {
                "name":        "api_key",
                "label":       "API Key",
                "type":        "text",
                "placeholder": "Your Dispatcharr API key",
                "required":    True,
                "help_text":   "Profile → API Key in Dispatcharr",
            },
            {
                "name":        "callback_url",
                "label":       "Webhook Callback URL",
                "type":        "text",
                "placeholder": "http://episeerr-host:5002/api/integration/dispatcharr/webhook",
                "required":    False,
                "help_text": (
                    "Episeerr URL that Dispatcharr will POST stream events to. "
                    "Must be reachable from Dispatcharr's host. "
                    "Leave blank to use poll-only mode (widget refreshes every 60 s)."
                ),
            },
        ]

    # ── Connection test ───────────────────────────────────────────

    def test_connection(self, url: str, api_key: str, **kwargs) -> Tuple[bool, str]:
        try:
            resp = requests.get(
                f"{url.rstrip('/')}/proxy/ts/status",
                headers={"X-Api-Key": api_key},
                timeout=5,
            )
            if resp.ok:
                count = resp.json().get("count", 0)
                noun  = "stream" if count == 1 else "streams"
                return True, f"Connected · {count} active {noun}"
            if resp.status_code == 401:
                return False, "Invalid API key — check Profile → API Key in Dispatcharr"
            if resp.status_code == 403:
                return False, "Forbidden — verify API key has sufficient permissions"
            return False, f"Unexpected HTTP {resp.status_code}"
        except requests.exceptions.ConnectionError:
            return False, "Cannot connect — verify URL and that Dispatcharr is running"
        except requests.exceptions.Timeout:
            return False, "Connection timed out"
        except Exception as exc:
            return False, str(exc)

    # ── Dashboard stats (pill data) ───────────────────────────────

    def get_dashboard_stats(self, url: str = None, api_key: str = None) -> Dict[str, Any]:
        # Bootstrap on first call: sync from API if we have no state yet
        if not _active_streams and _last_sync is None and url and api_key:
            _api_sync(url, api_key)
        with _lock:
            count = len(_active_streams)
        return {
            "configured":    True,
            "active_count":  count,
        }

    def get_dashboard_widget(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "pill": {
                "icon":       "fas fa-satellite-dish",
                "icon_color": "text-info",
                "template":   "{active_count} streaming",
                "fields":     ["active_count"],
            },
            "has_custom_widget": True,
        }

    # ── Post-save hook: auto-register webhook in Dispatcharr ──────

    def on_after_save(self, normalized_data: dict):
        """
        Called by episeerr after the user saves Dispatcharr config.
        Registers episeerr as a webhook integration in Dispatcharr and
        subscribes to all relevant stream events.  Falls back to poll-only
        mode if no callback_url was provided.
        """
        url         = (normalized_data.get("url") or "").rstrip("/")
        api_key     = normalized_data.get("api_key") or ""
        callback    = normalized_data.get("callback_url") or ""
        existing_id = normalized_data.get("webhook_integration_id")

        if not (url and api_key):
            return

        # Always do an initial sync so the pill shows current state immediately
        _bg_sync(url, api_key, delay=0.0)

        if not callback:
            logger.info("[Dispatcharr] No callback URL configured — poll-only mode")
            return

        # Register/update the webhook in a background thread so the save
        # response returns quickly to the user
        threading.Thread(
            target=self._register_webhook,
            args=(url, api_key, callback, existing_id, normalized_data),
            daemon=True,
            name="dispatcharr-webhook-register",
        ).start()

    def _register_webhook(
        self,
        url: str,
        api_key: str,
        callback: str,
        existing_id: Optional[int],
        original_config: dict,
    ):
        """Create or update the Dispatcharr webhook integration + event subscriptions."""
        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        base    = url.rstrip("/")

        events_to_subscribe = ["channel_start", "channel_stop"]

        try:
            integration_id = existing_id

            # Verify the existing registration is still valid
            if integration_id:
                check = requests.get(
                    f"{base}/api/connect/integrations/{integration_id}/",
                    headers=headers, timeout=5,
                )
                if not check.ok:
                    logger.info(f"[Dispatcharr] Stale webhook id={integration_id}, recreating")
                    integration_id = None

            # Create or update the integration record in Dispatcharr
            payload = {
                "name":    "Episeerr",
                "type":    "webhook",
                "config":  {"url": callback},
                "enabled": True,
            }

            if integration_id:
                resp = requests.patch(
                    f"{base}/api/connect/integrations/{integration_id}/",
                    json=payload, headers=headers, timeout=10,
                )
            else:
                resp = requests.post(
                    f"{base}/api/connect/integrations/",
                    json=payload, headers=headers, timeout=10,
                )

            if not resp.ok:
                logger.error(
                    f"[Dispatcharr] Webhook registration failed: "
                    f"{resp.status_code} {resp.text[:300]}"
                )
                return

            integration_id = resp.json().get("id")
            if not integration_id:
                logger.error("[Dispatcharr] No integration ID in response")
                return

            # Subscribe to each event individually
            ok_count = 0
            for event in events_to_subscribe:
                sub = requests.post(
                    f"{base}/api/connect/subscriptions/",
                    json={"event": event, "enabled": True, "integration": integration_id},
                    headers=headers, timeout=5,
                )
                if sub.ok:
                    ok_count += 1
                else:
                    logger.warning(
                        f"[Dispatcharr] Failed to subscribe to {event}: {sub.status_code}"
                    )

            logger.info(
                f"[Dispatcharr] Webhook registered (id={integration_id}, "
                f"{ok_count}/{len(events_to_subscribe)} events) → {callback}"
            )

            # Persist the webhook integration ID so future saves can update in-place
            try:
                from settings_db import get_service, save_service
                svc = get_service("dispatcharr") or {}
                new_config = {**original_config, "webhook_integration_id": integration_id}
                save_service(
                    service_type="dispatcharr",
                    name="default",
                    url=svc.get("url", url),
                    api_key=svc.get("api_key", api_key),
                    config=new_config,
                )
            except Exception as exc:
                logger.warning(f"[Dispatcharr] Could not persist webhook ID: {exc}")

        except Exception as exc:
            logger.error(f"[Dispatcharr] _register_webhook error: {exc}", exc_info=True)

    # ── Flask routes (self-contained blueprint) ───────────────────

    def create_blueprint(self) -> Blueprint:
        bp = Blueprint(
            "dispatcharr_integration", __name__,
            url_prefix="/api/integration/dispatcharr",
        )

        # ── Webhook receiver ──────────────────────────────────────
        @bp.route("/webhook", methods=["POST"])
        def webhook():
            """
            Receive channel_start / channel_stop events from Dispatcharr.

            In Dispatcharr: Connect → Integrations → Add Webhook
            URL:      http://<episeerr>:5002/api/integration/dispatcharr/webhook
            Triggers: Channel Started, Channel Stopped  (only these two needed)

            Accepts both naming variants Dispatcharr may use:
              channel_start / channel_started
              channel_stop  / channel_stopped
            """
            data  = request.get_json(silent=True) or {}
            event = data.get("event", "").lower()
            cid   = (
                data.get("channel_id") or
                data.get("channelId") or
                ""
            )

            logger.debug(f"[Dispatcharr] event={event!r} channel_id={cid!r}")

            cfg     = _get_saved_config()
            api_url = cfg.get("url", "") if cfg else ""
            api_key = cfg.get("api_key", "") if cfg else ""

            if event in ("channel_start", "channel_started"):
                name = (
                    data.get("stream_name") or
                    data.get("channel_name") or
                    data.get("channelName") or
                    data.get("name") or
                    "Unknown"
                )
                now = datetime.now(timezone.utc)
                with _lock:
                    _active_streams[cid] = {
                        "channel_id":   cid,
                        "channel_name": name,
                        "state":        "active",
                        "clients":      [],
                        "client_count": 0,
                        "uptime":       0,
                        "avg_bitrate":  "",
                        "resolution":   "",
                        "video_codec":  "",
                        "source_fps":   0,
                        "recording":    False,
                        "failover":     False,
                        "started_at":   now,
                        "last_seen":    now,
                    }
                logger.info(f"[Dispatcharr] Stream started: {name!r} ({cid})")
                if api_url and api_key:
                    _bg_sync(api_url, api_key, delay=1.5)  # fill in details after stream stabilises

            elif event in ("channel_stop", "channel_stopped"):
                with _lock:
                    removed = _active_streams.pop(cid, None)
                name = (removed or {}).get("channel_name", cid)
                logger.info(f"[Dispatcharr] Stream stopped: {name!r} ({cid})")

            else:
                logger.debug(f"[Dispatcharr] Ignored unhandled event: {event!r}")

            return jsonify({"status": "ok"}), 200

        # ── Dashboard widget HTML ─────────────────────────────────
        @bp.route("/widget")
        def widget():
            """
            Return live-stream widget HTML injected into the dashboard.

            Staleness guard: if any stream's last_seen is older than 5 minutes
            (missed stop webhook — app crash, network drop, etc.), a synchronous
            API resync is done before rendering.  Streams the API no longer
            reports are dropped, so the widget always reflects reality.
            """
            cfg = _get_saved_config()
            api_url = cfg.get("url", "") if cfg else ""
            api_key = cfg.get("api_key", "") if cfg else ""

            now = datetime.now(timezone.utc)
            stale_cutoff = timedelta(minutes=5)

            with _lock:
                stream_count = len(_active_streams)
                stale = [
                    cid for cid, s in _active_streams.items()
                    if (now - s.get("last_seen", now)) > stale_cutoff
                ]

            if not _active_streams:
                # Empty after restart — bootstrap from API
                if api_url and api_key:
                    _api_sync(api_url, api_key)
            elif stale and api_url and api_key:
                # One or more streams haven't been confirmed in 5+ minutes.
                # Resync from the API; streams no longer active will be absent
                # from the response and thus dropped from _active_streams.
                logger.info(
                    f"[Dispatcharr] {len(stale)}/{stream_count} stream(s) stale "
                    f"(>5 min since last_seen) — resyncing from API"
                )
                _api_sync(api_url, api_key)

            with _lock:
                post_sync_count = len(_active_streams)

            if stale and post_sync_count < stream_count:
                logger.info(
                    f"[Dispatcharr] Staleness check cleared "
                    f"{stream_count - post_sync_count} ghost stream(s)"
                )

            return jsonify({"success": True, "html": _render_widget()})

        # ── Force re-sync ─────────────────────────────────────────
        @bp.route("/sync", methods=["POST"])
        def manual_sync():
            """Force a full state refresh from the Dispatcharr API."""
            cfg = _get_saved_config()
            if not cfg:
                return jsonify({"status": "error", "message": "Not configured"}), 400
            ok = _api_sync(cfg.get("url", ""), cfg.get("api_key", ""))
            with _lock:
                count = len(_active_streams)
            return jsonify({
                "status":       "ok" if ok else "error",
                "active_count": count,
            })

        # ── Debug status ──────────────────────────────────────────
        @bp.route("/status")
        def status():
            """Return raw active-stream state as JSON (for debugging)."""
            with _lock:
                streams = {
                    cid: {
                        **s,
                        "uptime_fmt": _fmt_uptime(s.get("uptime", 0)),
                        "started_at": (
                            s["started_at"].isoformat()
                            if s.get("started_at") else None
                        ),
                    }
                    for cid, s in _active_streams.items()
                }
            return jsonify({
                "active_count": len(streams),
                "last_sync":    _last_sync.isoformat() if _last_sync else None,
                "streams":      streams,
            })

        return bp


# Auto-discovery: episeerr scans for this module-level variable
integration = DispatcharrIntegration()
