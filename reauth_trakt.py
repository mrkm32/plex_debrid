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

def main():
    print("\n" + "=" * 60)
    print("plex_debrid Trakt Re-Authorization")
    print("=" * 60)
    print("Requesting device code from Trakt...")
    sys.stdout.flush()

    try:
        r = requests.post(
            "https://api.trakt.tv/oauth/device/code",
            json={"client_id": CLIENT_ID},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Error connecting to Trakt: {e}")
        sys.exit(1)

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
                headers={"Content-Type": "application/json"},
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
            elif poll_r.status_code == 429:
                # Polling too quickly, wait interval
                time.sleep(interval)
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

    # Restart launchd daemon
    print("Restarting plex_debrid launchd daemon...")
    try:
        uid = os.getuid()
        res = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/com.plexdebrid.daemon"],
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
