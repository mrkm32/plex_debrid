#!/usr/bin/env python3
import sys
import os
import time
import json
import subprocess
import requests

CLIENT_ID = "pHbVNzLR5da9P4-GODsYtV6rZohyyhyLgCH73LQK6R0"
CLIENT_SECRET = "5Jmd-x75KhP2BXaouHosxkoITiz7gR5fPDVc2ERTrDA"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
ALERTS_STATE_PATH = os.path.join(BASE_DIR, ".alerts_state.json")
DAEMON_LABEL = "com.plexdebrid.daemon"
DAEMON_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{DAEMON_LABEL}.plist")


def _is_rate_limited(resp):
    """Return True for Trakt 429 or Cloudflare Error 1015 (HTTP 403/429)."""
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        body = resp.text.lower()
        return "1015" in body or "rate limited" in body
    return False


def _wait_for_rate_limit(resp, context="request"):
    """Wait out a 429 or Cloudflare 1015 rate limit with a visible countdown."""
    raw_retry = resp.headers.get("Retry-After")
    retry_after = 60
    if raw_retry is not None:
        try:
            retry_after = max(1, int(raw_retry))
        except (TypeError, ValueError):
            retry_after = 60
    kind = "Cloudflare Error 1015" if resp.status_code == 403 else "Trakt 429"
    print(f"\n[!] {kind} rate limit hit during {context} (HTTP {resp.status_code}). Waiting {retry_after}s...")
    sys.stdout.flush()
    remaining = retry_after
    while remaining > 0:
        step = min(15, remaining)
        print(f"    {remaining}s remaining...")
        sys.stdout.flush()
        time.sleep(step)
        remaining -= step


def main():
    print("\n" + "=" * 60)
    print("plex_debrid Trakt Re-Authorization")
    print("=" * 60)

    # Stop background daemon so it doesn't compete or trigger rate limits during OAuth
    uid = os.getuid()
    print(f"Stopping {DAEMON_LABEL} daemon before OAuth...")
    sys.stdout.flush()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{DAEMON_LABEL}"],
        capture_output=True,
        text=True,
    )

    print("Requesting device code from Trakt...")
    sys.stdout.flush()

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "trakt-api-key": CLIENT_ID,
        "trakt-api-version": "2"
    }

    data = None
    for attempt in range(5):
        try:
            r = requests.post(
                "https://api.trakt.tv/oauth/device/code",
                json={"client_id": CLIENT_ID},
                headers=headers,
                timeout=15
            )
            if _is_rate_limited(r):
                _wait_for_rate_limit(r, context="device code request")
                continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if attempt == 4:
                print(f"Error connecting to Trakt: {e}")
                sys.exit(1)
            time.sleep(3)

    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_url = data.get("verification_url", "https://trakt.tv/activate")
    expires_in = data.get("expires_in", 600)
    interval = data.get("interval", 6)

    print("\n" + "*" * 60)
    print(f" ACTION REQUIRED:")
    print(f" 1. Go to: {verification_url}")
    print(f" 2. Enter code: {user_code}")
    print(f" 3. Make sure you are logged into Trakt as 'mrkm32'")
    print(f" 4. Click 'Authorize'")
    print("*" * 60 + "\n")
    print(f"Waiting for your authorization (expires in {expires_in // 60} minutes)...")
    sys.stdout.flush()

    poll_payload = {
        "code": device_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }

    start_time = time.time()
    access_token = None

    while time.time() - start_time < expires_in:
        time.sleep(interval)
        try:
            poll_r = requests.post(
                "https://api.trakt.tv/oauth/device/token",
                json=poll_payload,
                headers=headers,
                timeout=10
            )
            if poll_r.status_code == 200:
                token_data = poll_r.json()
                access_token = token_data.get("access_token")
                print("\n[✓] Authorization successful!")
                sys.stdout.flush()
                break
            elif poll_r.status_code == 400:
                # Pending approval
                sys.stdout.write(".")
                sys.stdout.flush()
                continue
            elif poll_r.status_code == 404:
                print("\n[!] Device code not found or expired.")
                sys.exit(1)
            elif poll_r.status_code == 409:
                print("\n[!] Code already used.")
                sys.exit(1)
            elif poll_r.status_code == 410:
                print("\n[!] Code expired. Please rerun the script.")
                sys.exit(1)
            elif poll_r.status_code == 418:
                print("\n[!] User denied authorization.")
                sys.exit(1)
            elif _is_rate_limited(poll_r):
                _wait_for_rate_limit(poll_r, context="token polling")
                continue
            else:
                sys.stdout.write(f"\n[?] Unexpected response: {poll_r.status_code}\n")
                sys.stdout.flush()
        except requests.RequestException as e:
            sys.stdout.write(f"\n[!] Network issue: {e}\n")
            sys.stdout.flush()

    if not access_token:
        print("\n[!] Timed out waiting for authorization.")
        sys.exit(1)

    # Verify user identity
    print("Retrieving Trakt user profile...")
    sys.stdout.flush()
    username = "mrkm32"
    try:
        user_r = requests.get(
            "https://api.trakt.tv/users/me",
            headers={
                "Content-Type": "application/json",
                "trakt-api-key": CLIENT_ID,
                "trakt-api-version": "2",
                "Authorization": f"Bearer {access_token}"
            },
            timeout=10
        )
        if user_r.status_code == 200:
            user_data = user_r.json()
            username = user_data.get("username", "mrkm32")
            print(f"[✓] Authenticated as user: {username}")
    except Exception as e:
        print(f"Warning: Could not fetch username: {e}. Defaulting to 'mrkm32'.")

    # Update settings.json
    print(f"Updating {SETTINGS_PATH}...")
    try:
        with open(SETTINGS_PATH, "r") as f:
            settings = json.load(f)

        # Update Trakt users
        settings["Trakt users"] = [[username, access_token]]
        settings["Trakt library user"] = [username, access_token]
        settings["Trakt refresh user"] = [username, access_token]
        if "mrkm32's watchlist" in settings.get("Trakt lists", []):
            settings["Trakt lists"] = [f"{username}'s watchlist"]

        with open(SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=4)

        print("[✓] settings.json updated successfully.")
    except Exception as e:
        print(f"[!] Failed to update settings.json: {e}")
        sys.exit(1)

    # Clear trakt_401 alert state so future alerts work cleanly
    try:
        if os.path.exists(ALERTS_STATE_PATH):
            with open(ALERTS_STATE_PATH, "r") as f:
                alert_state = json.load(f)
            if "trakt_401" in alert_state:
                alert_state.pop("trakt_401", None)
                with open(ALERTS_STATE_PATH, "w") as f:
                    json.dump(alert_state, f, indent=2)
                print("[✓] Cleared trakt_401 cooldown state in .alerts_state.json.")
    except Exception as e:
        print(f"Warning: Could not update .alerts_state.json: {e}")

    # Restart launchd daemon
    print("Restarting plex_debrid launchd daemon...")
    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", DAEMON_PLIST],
            capture_output=True,
            text=True
        )
        res = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{DAEMON_LABEL}"],
            capture_output=True,
            text=True
        )
        if res.returncode == 0:
            print("[✓] Daemon successfully restarted!")
        else:
            print(f"[!] launchctl returned code {res.returncode}: {res.stderr.strip()}")
    except Exception as e:
        print(f"[!] Failed to restart daemon: {e}")

    print("\nDone! plex_debrid should now be syncing without 401 errors.")

if __name__ == "__main__":
    main()
