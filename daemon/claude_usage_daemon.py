#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (BLE) — macOS port of claude-usage-daemon.sh.

Option A (preferred): reads OAuth token from macOS Keychain or
~/.claude/.credentials.json, polls GET /api/oauth/usage.
Option B (fallback): extracts sessionKey cookie from Safari / Chrome /
Chromium / Brave / Firefox, polls claude.ai org usage endpoint.

Writes a JSON payload to the ESP32 "Claude Controller" peripheral over a
custom GATT service. Uses bleak (CoreBluetooth backend on macOS).
"""

import asyncio
import datetime
import getpass
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

DEVICE_NAME = "Claude Controller"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
SCAN_TIMEOUT = 8.0

# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux: token lives in ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
WEB_ORG_URL = "https://claude.ai/api/organizations"

# OAuth token refresh (same public client Claude Code itself uses).
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_REFRESH_SKEW_MS = 5 * 60 * 1000  # refresh 5 min before expiry
MAX_BACKOFF = 300  # cap 429 retry-after so shutdown stays responsive


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # new spec: {"oauth": {"access_token": "..."}}
        if isinstance(data.get("oauth"), dict) and isinstance(data["oauth"].get("access_token"), str):
            return data["oauth"]["access_token"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_credentials_blob() -> str | None:
    """Read the raw credentials blob from Keychain (macOS) or disk (Linux)."""
    if sys.platform != "darwin":
        try:
            return CREDENTIALS_PATH.read_text()
        except OSError as e:
            log(f"Error reading credentials: {e}")
            return None
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return out.stdout


def _write_credentials_blob(blob: str) -> bool:
    """Persist a refreshed credentials blob back to Keychain / disk.

    Note: on macOS the blob is passed to `security` via argv, so it is briefly
    visible to other processes owned by this user. Those processes can already
    read the same secret straight out of the Keychain, so this does not widen
    the trust boundary.
    """
    if sys.platform != "darwin":
        try:
            CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
            CREDENTIALS_PATH.write_text(blob)
            CREDENTIALS_PATH.chmod(0o600)
            return True
        except OSError as e:
            log(f"Error writing credentials: {e}")
            return False
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",  # update if the item already exists
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
                blob,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"Keychain write failed (rc={e.returncode}): {e.stderr.strip()}")
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain write error: {e}")
        return False


def _find_oauth_node(data: object) -> dict | None:
    """Locate the dict holding accessToken/refreshToken in a credentials blob."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("accessToken"), str):
        return data
    for v in data.values():
        if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
            return v
    return None


def _parse_credentials(blob: str) -> tuple[dict | None, dict | None]:
    """Return (whole_blob_dict, oauth_node) if the blob is structured JSON."""
    try:
        data = json.loads(blob.strip())
    except (json.JSONDecodeError, AttributeError):
        return None, None
    return (data if isinstance(data, dict) else None), _find_oauth_node(data)


async def _refresh_oauth_token(blob_data: dict, oauth: dict) -> str | None:
    """Exchange refreshToken for a new accessToken; persist and return it."""
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        log("OAuth: no refresh token stored — run 'claude login'")
        return None

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(OAUTH_TOKEN_URL, json=payload)
    except httpx.HTTPError as e:
        log(f"OAuth refresh failed: {e}")
        return None

    if resp.status_code != 200:
        log(f"OAuth refresh rejected (HTTP {resp.status_code}) — run 'claude login'")
        return None

    try:
        new = resp.json()
    except ValueError:
        log("OAuth refresh: unparseable response")
        return None

    access = new.get("access_token")
    if not isinstance(access, str) or not access:
        log("OAuth refresh: response had no access_token")
        return None

    oauth["accessToken"] = access
    if isinstance(new.get("refresh_token"), str):
        oauth["refreshToken"] = new["refresh_token"]
    expires_in = new.get("expires_in")
    if isinstance(expires_in, (int, float)):
        oauth["expiresAt"] = int((time.time() + expires_in) * 1000)

    if _write_credentials_blob(json.dumps(blob_data)):
        log("OAuth: token refreshed")
    else:
        log("OAuth: token refreshed but could not be persisted")
    return access


async def read_token() -> str | None:
    """Return a currently-valid access token, refreshing it if needed.

    Returns None when no usable token exists, so the caller can fall back to
    the browser-cookie path instead of hammering the API with a dead token.
    """
    blob = _read_credentials_blob()
    if not blob or not blob.strip():
        return None

    blob_data, oauth = _parse_credentials(blob)
    if blob_data is None or oauth is None:
        # Unstructured blob (raw token or unexpected shape): no expiry to check.
        return _extract_access_token(blob)

    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)):
        # expiresAt is milliseconds since epoch. Refresh a little early so a
        # token doesn't expire mid-poll.
        if time.time() * 1000 >= expires_at - TOKEN_REFRESH_SKEW_MS:
            return await _refresh_oauth_token(blob_data, oauth)

    access = oauth.get("accessToken")
    return access if isinstance(access, str) and access else None


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


def save_address(addr: str) -> None:
    SAVED_ADDR_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAVED_ADDR_FILE.write_text(addr)


async def scan_for_device() -> str | None:
    log(f"Scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    for d in devices:
        if d.name == DEVICE_NAME:
            log(f"Found: {d.address}")
            return d.address
    return None


def _build_payload(usage: dict) -> dict:
    """Convert /api/oauth/usage (or claude.ai org usage) JSON to BLE payload."""
    now = time.time()

    def mins_until(iso: str | None) -> int:
        if not iso:
            return -1
        try:
            ts = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
            return max(0, int((ts - now) / 60))
        except Exception:
            return -1

    # five_hour preferred; falls back to weekly buckets if 5h window absent
    primary = None
    for key in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"):
        w = usage.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            primary = w
            break

    week = None
    for key in ("seven_day", "seven_day_sonnet", "seven_day_opus"):
        w = usage.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            week = w
            break

    su = round(primary["utilization"]) if primary else 0
    sr = mins_until(primary.get("resets_at")) if primary else -1
    wu = round(week["utilization"]) if week else 0
    wr = mins_until(week.get("resets_at")) if week else -1
    st = "limited" if (su >= 100 or wu >= 100) else "allowed"

    tz_min = int(datetime.datetime.now(datetime.timezone.utc).astimezone()
                 .utcoffset().total_seconds() / 60)
    return {"s": su, "sr": sr, "w": wu, "wr": wr, "st": st,
            "ts": int(now), "tz": tz_min, "ok": True}


async def poll_oauth(token: str, stop_event: asyncio.Event | None = None) -> dict | None:
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "claude-code/2.1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(USAGE_API_URL, headers=headers)
    except httpx.HTTPError as e:
        log(f"OAuth poll failed: {e}")
        return None

    if resp.status_code == 401:
        log("OAuth: token expired — run 'claude login'")
        return None
    if resp.status_code == 403:
        log("OAuth: wrong token scope — run 'claude setup-token'")
        return None
    if resp.status_code == 429:
        retry = resp.headers.get("retry-after", "60")
        delay = min(float(retry) if retry.isdigit() else 60, MAX_BACKOFF)
        log(f"OAuth: rate limited, backing off {delay:.0f}s")
        # Race the shutdown flag — a bare sleep here made SIGTERM take up to an
        # hour to land (the daemon appeared to hang and needed kill -9).
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(delay)
        return None
    if resp.status_code != 200:
        log(f"OAuth: HTTP {resp.status_code}")
        return None

    try:
        return _build_payload(resp.json())
    except Exception as e:
        log(f"OAuth: payload parse failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Option B: browser sessionKey cookie
# ---------------------------------------------------------------------------

def _read_safari_cookie() -> str | None:
    path = Path.home() / "Library" / "Cookies" / "Cookies.binarycookies"
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
        if data[:4] != b"cook":
            return None
        num_pages = struct.unpack(">I", data[4:8])[0]
        page_sizes = struct.unpack(">" + "I" * num_pages, data[8:8 + num_pages * 4])
        offset = 8 + num_pages * 4
        for page_size in page_sizes:
            page = data[offset:offset + page_size]
            offset += page_size
            if len(page) < 8:
                continue
            num_cookies = struct.unpack("<I", page[4:8])[0]
            if not num_cookies or len(page) < 8 + num_cookies * 4:
                continue
            cookie_offsets = struct.unpack("<" + "I" * num_cookies, page[8:8 + num_cookies * 4])
            for co in cookie_offsets:
                try:
                    if co + 32 > len(page):
                        continue
                    domain_off = struct.unpack_from("<I", page, co + 16)[0]
                    name_off   = struct.unpack_from("<I", page, co + 20)[0]
                    value_off  = struct.unpack_from("<I", page, co + 28)[0]
                    if co + domain_off >= len(page) or co + name_off >= len(page) or co + value_off >= len(page):
                        continue
                    domain = page[co + domain_off:].split(b"\x00")[0].decode("utf-8", "ignore")
                    name   = page[co + name_off:].split(b"\x00")[0].decode("utf-8", "ignore")
                    value  = page[co + value_off:].split(b"\x00")[0].decode("utf-8", "ignore")
                    if "claude.ai" in domain and name == "sessionKey" and value.startswith("sk-ant-"):
                        return value
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _chrome_decrypt(key: bytes, encrypted: bytes) -> str | None:
    if not encrypted:
        return None
    if encrypted[:3] != b"v10":
        v = encrypted.decode("utf-8", "ignore")
        return v if v.startswith("sk-ant-") else None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        iv = b" " * 16  # Chrome uses all-space IV with PBKDF2-SHA1 key derivation
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        dec = cipher.decryptor()
        plain = dec.update(encrypted[3:]) + dec.finalize()
        pad = plain[-1]
        v = plain[:-pad].decode("utf-8", "ignore")
        return v if v.startswith("sk-ant-") else None
    except Exception:
        return None


def _read_chrome_cookie(db_path: str, keychain_service: str) -> str | None:
    if not os.path.exists(db_path):
        return None
    key: bytes | None = None
    try:
        pw = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", keychain_service],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        key = hashlib.pbkdf2_hmac("sha1", pw.encode(), b"saltysalt", 1003, 16)
    except Exception:
        pass

    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        row = conn.execute(
            "SELECT value, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%claude.ai' AND name='sessionKey' "
            "ORDER BY last_access_utc DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            plain = row[0]
            if plain and plain.startswith("sk-ant-"):
                return plain
            if row[1] and key:
                return _chrome_decrypt(key, row[1])
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None


def _read_firefox_cookie() -> str | None:
    for db in glob.glob(str(Path.home() / "Library/Application Support/Firefox/Profiles/*/cookies.sqlite")):
        fd, tmp = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            shutil.copy2(db, tmp)
            conn = sqlite3.connect(tmp)
            row = conn.execute(
                "SELECT value FROM moz_cookies "
                "WHERE host LIKE '%claude.ai' AND name='sessionKey' "
                "ORDER BY lastAccessed DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row and row[0].startswith("sk-ant-"):
                return row[0]
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return None


def read_cookie() -> str | None:
    """Search macOS browsers for a valid claude.ai sessionKey cookie.

    Order: Safari → Chrome → Chromium → Brave → Firefox.
    """
    base = str(Path.home() / "Library/Application Support")
    checks: list[tuple] = [
        (_read_safari_cookie, []),
        (_read_chrome_cookie, [f"{base}/Google/Chrome/Default/Cookies",      "Chrome Safe Storage"]),
        (_read_chrome_cookie, [f"{base}/Chromium/Default/Cookies",            "Chromium Safe Storage"]),
        (_read_chrome_cookie, [f"{base}/BraveSoftware/Brave-Browser/Default/Cookies", "Brave Safe Storage"]),
        (_read_firefox_cookie, []),
    ]
    for fn, args in checks:
        try:
            val = fn(*args)
            if val:
                return val
        except Exception:
            pass
    return None


async def poll_via_cookie(cookie: str) -> dict | None:
    headers = {"Cookie": f"sessionKey={cookie}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            orgs_resp = await http.get(WEB_ORG_URL, headers=headers)
            if orgs_resp.status_code in (401, 403):
                log("Cookie: session expired — log in to claude.ai in your browser")
                return None
            if orgs_resp.status_code != 200:
                log(f"Cookie: orgs HTTP {orgs_resp.status_code}")
                return None

            orgs = orgs_resp.json()
            if not isinstance(orgs, list) or not orgs:
                log("Cookie: no orgs in response")
                return None

            org_id = None
            for o in orgs:
                if "chat" in o.get("capabilities", []):
                    org_id = o["uuid"]; break
            if not org_id:
                for o in orgs:
                    if o.get("capabilities") != ["api"]:
                        org_id = o["uuid"]; break
            if not org_id:
                org_id = orgs[0]["uuid"]

            usage_resp = await http.get(
                f"https://claude.ai/api/organizations/{org_id}/usage",
                headers=headers,
            )
            if usage_resp.status_code in (401, 403):
                log("Cookie: session expired")
                return None
            if usage_resp.status_code != 200:
                log(f"Cookie: usage HTTP {usage_resp.status_code}")
                return None

            return _build_payload(usage_resp.json())

    except httpx.HTTPError as e:
        log(f"Cookie poll failed: {e}")
        return None
    except Exception as e:
        log(f"Cookie: payload parse failed: {e}")
        return None


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except BleakError as e:
            log(f"Write failed: {e}")
            return False


async def connect_and_run(address: str, stop_event: asyncio.Event) -> bool:
    """Connect to a known address and poll until disconnected or stopped.

    Returns True if the connection was used successfully (so the caller
    keeps the cached address), False if the connection failed and the
    cache should be invalidated.
    """
    log(f"Connecting to {address}...")
    client = BleakClient(address)
    try:
        await client.connect()
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                # Rate-limit the *attempt*, not just the success. Updating
                # last_poll only on success left this branch permanently true
                # and retried every TICK (5s) instead of every POLL_INTERVAL,
                # which is what earned the 429s in the first place.
                last_poll = time.time()
                token = await read_token()
                cookie = None if token else read_cookie()
                if not token and not cookie:
                    log("No credentials; skipping poll (try 'claude login' or log in to claude.ai)")
                else:
                    payload = (await poll_oauth(token, stop_event) if token
                               else await poll_via_cookie(cookie))
                    if payload is not None:
                        if await session.write_payload(payload):
                            last_poll = time.time()
                            used_successfully = True

            try:
                await asyncio.wait_for(session.refresh_requested.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    log("=== Claude Usage Tracker Daemon (BLE, macOS) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    backoff = 1
    while not stop_event.is_set():
        address = load_cached_address()
        if not address:
            address = await scan_for_device()
            if address:
                save_address(address)
            else:
                log(f"Device not found, retrying in {backoff}s...")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)
                continue

        ok = await connect_and_run(address, stop_event)
        if not ok:
            log("Invalidating cached address")
            SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
