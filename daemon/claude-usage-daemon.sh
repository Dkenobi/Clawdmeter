#!/bin/bash
# Claude Usage Tracker Daemon (BLE)
# Polls /api/oauth/usage (Option A: OAuth token) or claude.ai org usage endpoint
# (Option B: browser sessionKey cookie) and sends JSON over BLE GATT to ESP32.
# Auto-connects and reconnects to the Claude Controller BLE device.
# Dependencies: curl, python3, bluetoothctl

DEVICE_NAME="Claude Controller"
DEVICE_MAC="${DEVICE_MAC:-}"  # auto-discovered if empty
SERVICE_UUID="4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID="4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID="4c41555a-4465-7669-6365-000000000004"
POLL_INTERVAL=60
TICK=5
SAVED_MAC_FILE="$HOME/.config/claude-usage-monitor/ble-address"
REFRESH_FLAG="/tmp/claude-usage-refresh-$$"
DBUS_DEST="org.bluez"
NOTIFY_PID=""

log() {
    echo "[$(date '+%H:%M:%S')] $1"
}

read_token() {
    local creds="$HOME/.claude/.credentials.json"
    [ -f "$creds" ] || return 1
    local tok
    tok=$(python3 -c "
import json, sys
d = json.load(open('$creds'))
print(d.get('oauth', {}).get('access_token', '') or d.get('accessToken', ''))
" 2>/dev/null)
    [[ "$tok" == sk-ant-* ]] && echo "$tok" || return 1
}

# Extract sessionKey cookie from browser (Firefox → Chrome/Chromium → Brave)
read_cookie() {
    local cookie=""

    # Firefox: no encryption on Linux; copy DB first to avoid lock
    local ff_db
    ff_db=$(find "$HOME/.mozilla/firefox" -name "cookies.sqlite" 2>/dev/null | head -1)
    if [ -n "$ff_db" ]; then
        cookie=$(python3 - "$ff_db" <<'PYEOF'
import sqlite3, shutil, tempfile, os, sys
src = sys.argv[1]
tmp = tempfile.mktemp(suffix='.sqlite')
shutil.copy2(src, tmp)
try:
    conn = sqlite3.connect(tmp)
    row = conn.execute(
        "SELECT value FROM moz_cookies WHERE host LIKE '%claude.ai' AND name='sessionKey'"
        " ORDER BY lastAccessed DESC LIMIT 1"
    ).fetchone()
    if row: print(row[0])
finally:
    conn.close()
    os.unlink(tmp)
PYEOF
)
        [[ "$cookie" == sk-ant-* ]] && echo "$cookie" && return 0
    fi

    # Chrome / Chromium / Brave (plain-text value only; encrypted values skipped)
    local chrome_dir
    for chrome_dir in \
        "$HOME/.config/google-chrome" \
        "$HOME/.config/chromium" \
        "$HOME/.config/BraveSoftware/Brave-Browser"; do
        local db="$chrome_dir/Default/Cookies"
        [ -f "$db" ] || continue
        cookie=$(python3 - "$db" <<'PYEOF'
import sqlite3, shutil, tempfile, os, sys
src = sys.argv[1]
tmp = tempfile.mktemp(suffix='.sqlite')
shutil.copy2(src, tmp)
try:
    conn = sqlite3.connect(tmp)
    row = conn.execute(
        "SELECT value FROM cookies WHERE host_key LIKE '%claude.ai' AND name='sessionKey'"
        " ORDER BY last_access_utc DESC LIMIT 1"
    ).fetchone()
    if row and row[0]: print(row[0])
finally:
    conn.close()
    os.unlink(tmp)
PYEOF
)
        [[ "$cookie" == sk-ant-* ]] && echo "$cookie" && return 0
    done

    return 1
}

# Parse /api/oauth/usage (or /api/organizations/{id}/usage) JSON into BLE payload.
# Usage: _build_payload <json_body> <now_epoch>
_build_payload() {
    local body="$1"
    local now="$2"
    USAGE_JSON="$body" USAGE_NOW="$now" python3 - <<'PYEOF'
import json, os, sys
from datetime import datetime

body = json.loads(os.environ['USAGE_JSON'])
now = int(os.environ['USAGE_NOW'])

def mins_until(iso):
    if not iso: return -1
    try:
        ts = datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()
        return max(0, int((ts - now) / 60))
    except:
        return -1

sess = None
for key in ('five_hour', 'seven_day', 'seven_day_sonnet', 'seven_day_opus'):
    w = body.get(key)
    if w and w.get('utilization') is not None:
        sess = w
        break

week = None
for key in ('seven_day', 'seven_day_sonnet', 'seven_day_opus'):
    w = body.get(key)
    if w and w.get('utilization') is not None:
        week = w
        break

su = round(sess['utilization']) if sess else 0
sr = mins_until(sess.get('resets_at')) if sess else -1
wu = round(week['utilization']) if week else 0
wr = mins_until(week.get('resets_at')) if week else -1
st = 'limited' if (su >= 100 or wu >= 100) else 'allowed'

print('{"s":%d,"sr":%d,"w":%d,"wr":%d,"st":"%s","ok":true}' % (su, sr, wu, wr, st))
PYEOF
}

_poll_oauth() {
    local token="$1"
    local now
    now=$(date +%s)

    local response body http_code
    response=$(curl -s -w "\n%{http_code}" --max-time 30 \
        "https://api.anthropic.com/api/oauth/usage" \
        -H "Authorization: Bearer $token" \
        -H "anthropic-beta: oauth-2025-04-20" \
        -H "Accept: application/json" \
        -H "User-Agent: claude-code/2.1.0" \
        2>/dev/null)
    http_code=$(echo "$response" | tail -1)
    body=$(echo "$response" | head -n -1)

    case "$http_code" in
        200) ;;
        401) log "OAuth: token expired — run 'claude login'"; return 1 ;;
        403) log "OAuth: wrong token scope — run 'claude setup-token'"; return 1 ;;
        429)
            local retry_secs
            retry_secs=$(echo "$body" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('retry_after',60))" 2>/dev/null || echo 60)
            log "OAuth: rate limited, backing off ${retry_secs}s"
            sleep "$retry_secs"
            return 1 ;;
        *) log "OAuth: HTTP $http_code"; return 1 ;;
    esac

    local payload
    payload=$(_build_payload "$body" "$now") || { log "OAuth: payload parse failed"; return 1; }
    log "Sending: $payload"
    write_gatt "$RX_CHAR_PATH" "$payload" || { log "Write failed"; return 1; }
}

_poll_cookie() {
    local cookie="$1"
    local now
    now=$(date +%s)

    # Step 1: resolve org ID
    local orgs_body
    orgs_body=$(curl -s --max-time 15 \
        "https://claude.ai/api/organizations" \
        -H "Cookie: sessionKey=$cookie" \
        -H "Accept: application/json" \
        2>/dev/null)

    local org_id
    org_id=$(echo "$orgs_body" | python3 - <<'PYEOF'
import json, sys
orgs = json.loads(sys.stdin.read())
if not isinstance(orgs, list) or not orgs: sys.exit(1)
for o in orgs:
    if 'chat' in o.get('capabilities', []):
        print(o['uuid']); sys.exit(0)
for o in orgs:
    if o.get('capabilities') != ['api']:
        print(o['uuid']); sys.exit(0)
print(orgs[0]['uuid'])
PYEOF
)
    [ -z "$org_id" ] && { log "Cookie: could not resolve org ID"; return 1; }

    # Step 2: fetch usage
    local response body http_code
    response=$(curl -s -w "\n%{http_code}" --max-time 15 \
        "https://claude.ai/api/organizations/$org_id/usage" \
        -H "Cookie: sessionKey=$cookie" \
        -H "Accept: application/json" \
        2>/dev/null)
    http_code=$(echo "$response" | tail -1)
    body=$(echo "$response" | head -n -1)

    case "$http_code" in
        200) ;;
        401|403) log "Cookie: session expired — log in to claude.ai in your browser"; return 1 ;;
        *) log "Cookie: HTTP $http_code from usage endpoint"; return 1 ;;
    esac

    local payload
    payload=$(_build_payload "$body" "$now") || { log "Cookie: payload parse failed"; return 1; }
    log "Sending: $payload"
    write_gatt "$RX_CHAR_PATH" "$payload" || { log "Write failed"; return 1; }
}

# Convert MAC to D-Bus path: AA:BB:CC:DD:EE:FF -> dev_AA_BB_CC_DD_EE_FF
mac_to_dbus_path() {
    local adapter
    adapter=$(busctl call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects 2>/dev/null | grep -o '/org/bluez/hci[0-9]' | head -1)
    adapter=${adapter:-/org/bluez/hci0}
    echo "${adapter}/dev_$(echo "$1" | tr ':' '_')"
}

# Check if device is connected via D-Bus
is_connected() {
    local path
    path=$(mac_to_dbus_path "$DEVICE_MAC")
    busctl get-property "$DBUS_DEST" "$path" org.bluez.Device1 Connected 2>/dev/null | grep -q "true"
}

# Load saved MAC address
load_mac() {
    if [ -n "$DEVICE_MAC" ]; then return 0; fi
    if [ -f "$SAVED_MAC_FILE" ]; then
        DEVICE_MAC=$(head -1 "$SAVED_MAC_FILE" | tr -d '\r\n ')
        if [[ "$DEVICE_MAC" =~ ^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$ ]]; then
            return 0
        fi
        log "Cached MAC is malformed, discarding"
        rm -f "$SAVED_MAC_FILE"
        DEVICE_MAC=""
    fi
    return 1
}

# Save MAC for fast reconnect
save_mac() {
    mkdir -p "$(dirname "$SAVED_MAC_FILE")"
    echo "$DEVICE_MAC" > "$SAVED_MAC_FILE"
}

# Scan for Claude Controller
scan_for_device() {
    log "Scanning for '$DEVICE_NAME'..."
    # Start LE scan
    bluetoothctl scan le &>/dev/null &
    local scan_pid=$!
    sleep 8
    kill "$scan_pid" 2>/dev/null
    wait "$scan_pid" 2>/dev/null

    # Pick the first matching device. Multiple matches happen when bluez
    # remembers old hardware (e.g. after swapping ESP boards). Stale entries
    # are removed on connect failure (see connect_device), so a few retry
    # cycles will converge on the live device.
    local found
    found=$(bluetoothctl devices 2>/dev/null | grep "$DEVICE_NAME" | head -1 | awk '{print $2}')
    if [ -n "$found" ]; then
        DEVICE_MAC="$found"
        save_mac
        log "Found: $DEVICE_MAC"
        return 0
    fi
    return 1
}

# Connect to the device
connect_device() {
    log "Connecting to $DEVICE_MAC..."

    # Trust first (allows auto-reconnect)
    bluetoothctl trust "$DEVICE_MAC" &>/dev/null

    # Connect
    bluetoothctl connect "$DEVICE_MAC" &>/dev/null
    sleep 2

    if is_connected; then
        log "Connected"
        return 0
    fi
    log "Connection failed"
    if [ -f "$SAVED_MAC_FILE" ] && [ "$(cat "$SAVED_MAC_FILE")" = "$DEVICE_MAC" ]; then
        log "Invalidating cached MAC, will rescan by name"
        rm -f "$SAVED_MAC_FILE"
    fi
    # Remove from bluez so the next scan won't re-pick this dead MAC.
    # If the device comes back online it'll re-advertise and be re-discovered.
    bluetoothctl remove "$DEVICE_MAC" &>/dev/null
    DEVICE_MAC=""
    return 1
}

# Find a GATT characteristic path by UUID via D-Bus
find_char_path_by_uuid() {
    local target_uuid="$1"
    local dev_path
    dev_path=$(mac_to_dbus_path "$DEVICE_MAC")

    busctl tree "$DBUS_DEST" 2>/dev/null | grep -o "${dev_path}/service[0-9a-f]*/char[0-9a-f]*" | while read -r char_path; do
        local uuid
        uuid=$(busctl get-property "$DBUS_DEST" "$char_path" org.bluez.GattCharacteristic1 UUID 2>/dev/null | tr -d '"' | awk '{print $2}')
        if [ "$uuid" = "$target_uuid" ]; then
            echo "$char_path"
            return 0
        fi
    done
}

# Subscribe to refresh-request notifications. The ESP fires this when it
# has no usage data yet (e.g. after a fresh boot). Daemon awk drops a flag
# file that the inner loop picks up on its next 5s tick.
#
# Implementation notes:
# - dbus-monitor must be running BEFORE we call StartNotify, because busctl
#   exits immediately, the subscription tears down within milliseconds, and
#   the ESP's notify fires inside that brief window.
# - stdbuf -oL forces line-buffered stdout on dbus-monitor; without it,
#   glibc switches to block buffering when stdout is a pipe and signals
#   never reach awk until ~4KB accumulates.
# - The pipeline runs in a setsid'd child so we can kill the whole process
#   group (dbus-monitor + awk) atomically. Killing only awk leaves
#   dbus-monitor orphaned, and `wait $!` in bash waits on the whole job
#   until every pipeline member exits, hanging the daemon.
start_notify_subscriber() {
    local req_path
    req_path=$(find_char_path_by_uuid "$REQ_CHAR_UUID")
    if [ -z "$req_path" ]; then
        log "Refresh char not found, skipping notify subscriber"
        return 1
    fi

    setsid bash -c "stdbuf -oL dbus-monitor --system \"type='signal',interface='org.freedesktop.DBus.Properties',path='$req_path',member='PropertiesChanged'\" 2>/dev/null | awk -v flag='$REFRESH_FLAG' '/Value/ { system(\"touch \" flag); fflush() }'" &
    NOTIFY_PID=$!

    # Give dbus-monitor a moment to register its match rule, then trigger
    # the GATT subscription that causes the ESP to fire its notify.
    sleep 0.3
    busctl call "$DBUS_DEST" "$req_path" org.bluez.GattCharacteristic1 StartNotify >/dev/null 2>&1

    log "Refresh subscriber started (pgid=$NOTIFY_PID)"
}

stop_notify_subscriber() {
    if [ -n "$NOTIFY_PID" ]; then
        # Kill the whole process group (setsid made NOTIFY_PID the leader).
        # Don't wait — we don't care about exit status and waiting can hang
        # if any group member is slow to exit.
        kill -TERM -"$NOTIFY_PID" 2>/dev/null
        NOTIFY_PID=""
    fi
    rm -f "$REFRESH_FLAG"
}

# Write data to the RX characteristic via D-Bus
write_gatt() {
    local char_path="$1"
    local data="$2"

    # Convert string to byte array for D-Bus: "hi" -> 0x68 0x69
    local bytes=""
    for ((i = 0; i < ${#data}; i++)); do
        local byte
        byte=$(printf "0x%02x" "'${data:$i:1}")
        bytes="$bytes $byte"
    done
    local count=${#data}

    busctl call "$DBUS_DEST" "$char_path" org.bluez.GattCharacteristic1 \
        WriteValue "aya{sv}" "$count" $bytes 0 2>/dev/null
}

poll() {
    local token cookie
    if token=$(read_token); then
        _poll_oauth "$token" && return 0
    fi
    if cookie=$(read_cookie); then
        log "OAuth unavailable, falling back to browser cookie"
        _poll_cookie "$cookie" && return 0
    fi
    log "Error: no valid credentials — run 'claude login' or log in to claude.ai in your browser"
    return 1
}

cleanup() {
    stop_notify_subscriber
    log "Daemon stopped"
    exit 0
}

trap cleanup INT TERM

log "=== Claude Usage Tracker Daemon (BLE) ==="
log "Poll interval: ${POLL_INTERVAL}s"

BACKOFF=1

while true; do
    # Find the device
    if ! load_mac; then
        scan_for_device || {
            log "Device not found, retrying in ${BACKOFF}s..."
            sleep "$BACKOFF"
            BACKOFF=$((BACKOFF < 60 ? BACKOFF * 2 : 60))
            continue
        }
    fi

    # Connect if not connected
    if ! is_connected; then
        connect_device || {
            log "Retrying in ${BACKOFF}s..."
            sleep "$BACKOFF"
            BACKOFF=$((BACKOFF < 60 ? BACKOFF * 2 : 60))
            continue
        }
    fi

    # Find the GATT characteristic
    RX_CHAR_PATH=$(find_char_path_by_uuid "$RX_CHAR_UUID")
    if [ -z "$RX_CHAR_PATH" ]; then
        log "Error: RX characteristic not found, retrying..."
        sleep 5
        continue
    fi
    log "GATT RX path: $RX_CHAR_PATH"

    BACKOFF=1  # reset backoff on successful connection

    start_notify_subscriber

    # Poll loop: tick every $TICK seconds. Poll Anthropic when the
    # interval has elapsed OR when the ESP requested a refresh.
    LAST_POLL=0
    while is_connected; do
        NOW=$(date +%s)
        if [ -f "$REFRESH_FLAG" ] || (( NOW - LAST_POLL >= POLL_INTERVAL )); then
            if [ -f "$REFRESH_FLAG" ]; then
                log "Refresh requested by device"
                rm -f "$REFRESH_FLAG"
            fi
            poll && LAST_POLL=$NOW
        fi
        sleep "$TICK"
    done

    stop_notify_subscriber
    log "Device disconnected, reconnecting..."
    sleep 2
done
