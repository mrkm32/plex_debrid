#!/usr/bin/env python3
"""
Alert notification dispatcher for plex_debrid.
Supports native macOS iMessage dispatch via AppleScript, local desktop banners,
and cooldown-based debouncing to prevent notification spam.
"""

import sys
import os
import json
import time
import subprocess
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(BASE_DIR, "alerts_config.json")
STATE_FILE = os.path.join(BASE_DIR, ".alerts_state.json")

DEFAULT_CONFIG = {
    "enabled": True,
    "method": "imessage",
    "imessage_recipient": "matthews.mg@gmail.com",
    "cooldown_hours": 6,
    "macos_banner": True
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                merged = {**DEFAULT_CONFIG, **cfg}
                return merged
        except Exception as e:
            print(f"[Alerts] Warning: could not read {CONFIG_FILE}: {e}", file=sys.stderr)
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        print(f"[Alerts] Warning: could not write {CONFIG_FILE}: {e}", file=sys.stderr)

def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

def send_macos_banner(title: str, message: str):
    clean_title = title.replace('"', '\\"')
    clean_msg = message.replace('"', '\\"')
    script = f'display notification "{clean_msg}" with title "{clean_title}" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)
    except Exception as e:
        print(f"[Alerts] Failed to send macOS notification: {e}", file=sys.stderr)

def send_imessage(recipient: str, text: str) -> bool:
    # Escape quotes and backslashes for AppleScript
    clean_recipient = recipient.replace('\\', '\\\\').replace('"', '\\"')
    clean_text = text.replace('\\', '\\\\').replace('"', '\\"')
    
    script = f'''tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    set targetBuddy to participant "{clean_recipient}" of targetService
    send "{clean_text}" to targetBuddy
end tell'''
    
    try:
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            return True
        else:
            print(f"[Alerts] osascript error ({res.returncode}): {res.stderr.strip()}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"[Alerts] Exception sending iMessage: {e}", file=sys.stderr)
        return False

def send_alert(title: str, message: str, error_key: str = "general", force: bool = False) -> bool:
    """
    Sends an alert with rate-limiting / cooldown.
    Returns True if alert was sent, False if throttled by cooldown.
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return False

    cooldown_secs = cfg.get("cooldown_hours", 6) * 3600
    state = _load_state()
    last_sent = state.get(error_key, 0)
    now = time.time()

    if not force and (now - last_sent < cooldown_secs):
        remaining = int((cooldown_secs - (now - last_sent)) / 60)
        # Suppressed by cooldown
        return False

    # Format the message
    full_message = f"🚨 [{title}]\n{message}"

    delivered = False
    method = cfg.get("method", "imessage")
    recipient = cfg.get("imessage_recipient", "matthews.mg@gmail.com")

    if method == "imessage":
        delivered = send_imessage(recipient, full_message)
    else:
        # Fallback or additional methods can be wired here
        delivered = False

    if cfg.get("macos_banner", True):
        send_macos_banner(title, message)

    if delivered or force:
        state[error_key] = now
        _save_state(state)
        return True

    return delivered

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send plex_debrid alerts")
    parser.add_argument("--test", action="store_true", help="Send a test alert")
    parser.add_argument("--title", default="plex_debrid Alert", help="Alert title")
    parser.add_argument("--message", default="", help="Alert message")
    parser.add_argument("--key", default="cli_test", help="Error key for cooldown")
    parser.add_argument("--force", action="store_true", help="Bypass cooldown")
    args = parser.parse_args()

    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        print(f"Created default config at {CONFIG_FILE}")

    if args.test:
        print("Sending test alert...")
        ok = send_alert(
            title="plex_debrid Test Alert",
            message="This is a test notification. Alert dispatch is working!",
            error_key="test_notification",
            force=True
        )
        if ok:
            print("✓ Test alert sent successfully!")
        else:
            print("✗ Failed to send test alert.")
    elif args.message:
        ok = send_alert(args.title, args.message, error_key=args.key, force=args.force)
        print("Sent." if ok else "Suppressed by cooldown or failed.")
