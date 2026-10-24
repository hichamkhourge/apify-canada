"""
Update an Xtream playlist on the IPTV player's online playlist manager
(Cloudflare Worker, see lg/iptvplayer/cloud/README.md).

Environment Variables:
    PLAYER_API_URL        - Worker address (default: https://iptv-playlists.hi-lux-store.workers.dev)
    PLAYER_DEVICE_ID      - Device ID shown on the TV
    PLAYER_DEVICE_KEY     - Device key shown on the TV (sent as the Bearer token)
    PLAYER_PLAYLIST_ID    - ID of the playlist to update
    PLAYER_PLAYLIST_NAME  - (optional) new playlist name; the current name is kept otherwise
"""

import os
import time

import requests

DEFAULT_PLAYER_API_URL = "https://iptv-playlists.hi-lux-store.workers.dev"


def _config():
    return {
        "api_url": (os.getenv("PLAYER_API_URL") or DEFAULT_PLAYER_API_URL).strip().rstrip("/"),
        "device_id": (os.getenv("PLAYER_DEVICE_ID") or "").strip(),
        "device_key": (os.getenv("PLAYER_DEVICE_KEY") or "").strip(),
        "playlist_id": (os.getenv("PLAYER_PLAYLIST_ID") or "").strip(),
        "playlist_name": (os.getenv("PLAYER_PLAYLIST_NAME") or "").strip(),
    }


def player_enabled():
    """True when device ID, device key and playlist ID are all configured."""
    cfg = _config()
    return bool(cfg["device_id"] and cfg["device_key"] and cfg["playlist_id"])


def _describe_error(response):
    try:
        data = response.json()
        return f"{data.get('error')}: {data.get('message') or ''}".strip(": ")
    except Exception:
        return response.text[:300]


def _request(method, url, headers, max_retries, **kwargs):
    """Send a request, retrying timeouts and 5xx with exponential backoff. Returns the response or None."""
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
            if response.status_code < 500:
                return response
            print(f"[!] Server error ({response.status_code}): {_describe_error(response)}")
        except requests.exceptions.RequestException as e:
            print(f"[!] Request failed (attempt {attempt}/{max_retries}): {e}")

        if attempt < max_retries:
            wait_time = 2 ** attempt
            print(f"[*] Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    print("[!] Max retries reached.")
    return None


def update_player_playlist(server, username, password, max_retries=3):
    """
    Replace the server/username/password of the configured playlist on the player.

    The Worker's PUT replaces the whole playlist, so the current one is read first to keep
    its name and EPG URL.

    Returns:
        bool: True if the playlist was updated
    """
    cfg = _config()
    if not player_enabled():
        print("[*] Player update is disabled (PLAYER_DEVICE_ID / PLAYER_DEVICE_KEY / PLAYER_PLAYLIST_ID not set)")
        return False

    print("\n[*] Updating player playlist...")
    base = f"{cfg['api_url']}/api/devices/{cfg['device_id']}/playlists"
    headers = {"Authorization": f"Bearer {cfg['device_key']}"}

    print(f"[*] API URL: {base}/{cfg['playlist_id']}")

    response = _request("GET", base, headers, max_retries)
    if response is None:
        return False
    if response.status_code != 200:
        print(f"[!] Could not read playlists ({response.status_code}): {_describe_error(response)}")
        return False

    current = next((p for p in response.json().get("playlists", []) if p.get("id") == cfg["playlist_id"]), None)
    if not current:
        print(f"[!] Playlist {cfg['playlist_id']} not found on device {cfg['device_id']}")
        return False

    payload = {
        "type": "xtream",
        "server": server,
        "username": username,
        "password": password,
        "name": cfg["playlist_name"] or current.get("name") or "",
        "epgUrl": current.get("epgUrl") or "",
    }

    print(f"[*] Playlist Name: {payload['name']}")
    print(f"[*] Server: {server}")
    print(f"[*] Username: {username}")

    response = _request("PUT", f"{base}/{cfg['playlist_id']}", headers, max_retries, json=payload)
    if response is None:
        return False
    if response.status_code != 200:
        print(f"[!] Player rejected the update ({response.status_code}): {_describe_error(response)}")
        return False

    print(f"[✓] Player playlist updated successfully! Response: {response.text}")
    return True
