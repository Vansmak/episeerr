#!/bin/bash
# Sanctum IP failover watchdog — see maintenance.sql's header for the full incident this
# exists because of. u.veiltheworld.com resolves to a single, stable A record (198.255.96.85)
# via every resolver on this network (NextDNS, Cloudflare, the router) — this isn't a DNS
# problem and re-resolving the domain each run wouldn't reveal anything different, so this
# checks the known IPs directly rather than resolving the hostname.
#
# 2026-09 incident: .85 got blocked/refused at the network level (anti-abuse flag, since
# resolved) for an extended period. A second working IP for the same domain — 216.227.189.197 —
# stayed reachable the whole time; it surfaced only because the ISP's own DNS resolver happened
# to hand it out instead of .85 (a routing/ECS quirk, unrelated to the block). Nothing about the
# domain's real DNS records changed — this is a second real IP the service already answers on.
#
# What this does: every run, check whether the primary IP is reachable. If it isn't but the
# fallback is, pin the hostname to the fallback via a docker-compose override and recreate the
# dispatcharr container. Once the primary recovers, remove the override and recreate again.
# Logs only on an actual state change (switch, revert, or "both down" — a real outage, not a
# single-IP block) — a healthy check every run would make the log useless within a day.
#
# The check itself is a bare TCP connect (see check_tcp below) on a 15-minute cadence, not 5 —
# this runs forever against a provider whose abuse detection already flagged this account once,
# and an "extended period" outage doesn't need finer-grained detection than that to catch fast.
#
# Second signal, zero extra footprint: Dispatcharr already attempts a real authenticated M3U
# download from Sanctum on its own schedule (refresh_interval, currently every 12h) and records
# the outcome in m3u_m3uaccount.status/last_message. This watchdog reads that via a local psql
# query — no network request of its own — which catches failures a bare TCP connect can't see
# (a 403, an auth reject, a connection reset mid-download: all things that happen *after* a
# successful TCP handshake). See sanctum_refresh_freshly_failed() for why only a *new* error
# counts, not Dispatcharr's last attempt regardless of age.
#
# Install: crontab -e
#   */15 * * * * /home/joe/projects/episeerr_custom/scripts/dispatcharr/sanctum_failover_watchdog.sh

set -euo pipefail

DOMAIN="u.veiltheworld.com"
PRIMARY_IP="198.255.96.85"
FALLBACK_IP="216.227.189.197"
COMPOSE_FILE="$HOME/docker/media/docker-compose.yml"
OVERRIDE_FILE="$HOME/docker/media/docker-compose.override.yml"
LOG_FILE="$HOME/config/dispatcharr/sanctum_failover.log"
SANCTUM_ACCOUNT_ID=12
STATUS_STATE_FILE="$HOME/config/dispatcharr/sanctum_last_status_seen.txt"
TIMEOUT=6

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

check_tcp() {
    # Bare TCP connect/close via nc -z — no TLS handshake, no HTTP request, nothing that shows
    # up as an application-level hit in Sanctum's own access logs. This runs on a schedule,
    # forever, against a provider whose abuse detection already flagged this exact account once
    # (see the header) — a full HTTPS GET every few minutes would itself look like automated
    # probing. A bare connect is also all this needs: the actual Sept 2026 block was a pure
    # TCP-level refusal, logged by ffmpeg before any TLS/HTTP layer was ever reached.
    # bash's /dev/tcp wrapped in `timeout` was tried first and rejected — it doesn't reliably
    # interrupt a blocking connect() against this host's specific (DROP-style) block behavior,
    # confirmed while building this. nc's own -w timeout is enforced internally and does.
    nc -z -w "$TIMEOUT" "$1" 443 2>/dev/null
}

override_active() {
    [ -f "$OVERRIDE_FILE" ] && grep -q "$FALLBACK_IP" "$OVERRIDE_FILE" 2>/dev/null
}

# True only when Dispatcharr's own last refresh attempt for Sanctum ended in 'error' AND that
# attempt is new since the last time this script looked. Without the "new" check, a single old
# failure sits in status/last_message unchanged until Dispatcharr's next scheduled try (up to
# 12h later) and would look like an active problem on every run in between, even seconds after
# it actually resolved. Always records the current (status, updated_at) pair before returning,
# success or failure, so the comparison stays accurate on the next run either way.
sanctum_refresh_freshly_failed() {
    local row status updated_at last_seen
    row=$(docker exec dispatcharr psql -U postgres -d dispatcharr -tAc \
        "SELECT status || '|' || extract(epoch from updated_at)::bigint FROM m3u_m3uaccount WHERE id=$SANCTUM_ACCOUNT_ID;" 2>/dev/null) || return 1
    [ -z "$row" ] && return 1
    status="${row%%|*}"
    updated_at="${row##*|}"
    last_seen=$(cat "$STATUS_STATE_FILE" 2>/dev/null || echo "")
    echo "$updated_at" > "$STATUS_STATE_FILE"
    [ "$status" = "error" ] && [ "$updated_at" != "$last_seen" ]
}

# Kicks Dispatcharr's own refresh for Sanctum right after we switch/revert its DNS, instead of
# waiting up to 12h for the next scheduled attempt — confirms the fix worked (status flips back
# to success) and gets fresh channel data flowing again immediately. Calls the same Celery task
# the scheduler itself calls, via the Django shell, so no Dispatcharr API auth is needed.
trigger_sanctum_refresh() {
    docker exec dispatcharr python manage.py shell -c \
        "from apps.m3u.tasks import refresh_single_m3u_account; refresh_single_m3u_account.delay($SANCTUM_ACCOUNT_ID)" \
        >> "$LOG_FILE" 2>&1 || true
}

apply_fallback() {
    local reason="$1"
    cat > "$OVERRIDE_FILE" <<EOF
# Written by sanctum_failover_watchdog.sh — do not edit by hand, it will be
# overwritten or deleted automatically. See that script for why this exists.
services:
  dispatcharr:
    extra_hosts:
      - "$DOMAIN:$FALLBACK_IP"
EOF
    docker compose -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" up -d dispatcharr >> "$LOG_FILE" 2>&1
    log "SWITCHED TO FALLBACK: $DOMAIN -> $FALLBACK_IP ($reason)"
    trigger_sanctum_refresh
    curl -fsS -m 8 -X POST http://192.168.254.205:5002/api/notify \
        -H "Content-Type: application/json" \
        -d "{\"source\":\"Sanctum Failover\",\"title\":\"Switched to backup IP\",\"message\":\"$DOMAIN was unreachable on the usual IP ($reason); now pinned to the known-good fallback.\",\"type\":\"warning\"}" \
        >> "$LOG_FILE" 2>&1 || true
}

revert_to_primary() {
    rm -f "$OVERRIDE_FILE"
    docker compose -f "$COMPOSE_FILE" up -d dispatcharr >> "$LOG_FILE" 2>&1
    log "REVERTED TO PRIMARY: $DOMAIN -> normal DNS ($PRIMARY_IP recovered)"
    trigger_sanctum_refresh
    curl -fsS -m 8 -X POST http://192.168.254.205:5002/api/notify \
        -H "Content-Type: application/json" \
        -d "{\"source\":\"Sanctum Failover\",\"title\":\"Back on primary IP\",\"message\":\"$DOMAIN recovered; reverted to normal DNS.\",\"type\":\"info\"}" \
        >> "$LOG_FILE" 2>&1 || true
}

if override_active; then
    if check_tcp "$PRIMARY_IP"; then
        revert_to_primary
    fi
    # else: still on fallback, primary still down — nothing to do, nothing to log
else
    tcp_down=false
    check_tcp "$PRIMARY_IP" || tcp_down=true
    # Always evaluated (not short-circuited) so its state file gets updated every run regardless
    # of the TCP result above.
    app_layer_down=false
    sanctum_refresh_freshly_failed && app_layer_down=true

    if $tcp_down || $app_layer_down; then
        if check_tcp "$FALLBACK_IP"; then
            if $tcp_down; then
                apply_fallback "primary $PRIMARY_IP unreachable"
            else
                apply_fallback "Dispatcharr's own M3U refresh for Sanctum just failed"
            fi
        else
            log "WARNING: primary ($PRIMARY_IP) and fallback ($FALLBACK_IP) both unreachable — looks like a real outage, not a single-IP block. No action taken."
        fi
    fi
    # else: primary healthy by both signals — nothing to do, nothing to log
fi
