"""Configuration management for WatchNight."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

PROJECT_DIR = Path("/Users/markmatthews/plex_debrid")
SETTINGS_PATH = PROJECT_DIR / "settings.json"
RECOMMENDER_CONFIG_PATH = PROJECT_DIR / "recommender_config.json"
DATA_DIR = PROJECT_DIR / "webapp" / "data"
IMAGE_CACHE_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "watchnight.db"
STATIC_DIR = PROJECT_DIR / "webapp" / "static"

PORT = 8787
TRAKT_CLIENT_ID = "pHbVNzLR5da9P4-GODsYtV6rZohyyhyLgCH73LQK6R0"

AI_MODEL_PRESETS: dict[str, dict[str, Any]] = {
    "gemini": {
        "label": "Google Gemini",
        "default": "gemini-3.8-flash",
        "models": [
            "gemini-3.8-flash",
            "gemini-4-neon",
            "gemini-4-argon",
            "gemini-3.5-flash-lite",
        ],
    },
    "openai": {
        "label": "OpenAI",
        "default": "gpt-6.2-sol",
        "models": [
            "gpt-6.2-sol",
            "gpt-6.2-astra",
            "gpt-6.1-terra",
            "gpt-6-luna",
        ],
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "default": "claude-sonnet-5-5",
        "models": [
            "claude-sonnet-5-5",
            "claude-opus-5-5",
            "claude-haiku-5-5",
        ],
    },
}

_settings_lock = threading.Lock()
_settings_cache: dict[str, Any] = {"mtime": None, "data": {}}
_config_lock = threading.Lock()


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_plex_debrid_settings(settings_path: Path | None = None) -> dict[str, Any]:
    """Load plex_debrid settings.json with mtime caching."""
    path = settings_path or SETTINGS_PATH
    if not path.exists():
        return {}
    with _settings_lock:
        try:
            mtime = path.stat().st_mtime
            if path == SETTINGS_PATH and _settings_cache["mtime"] == mtime and _settings_cache["data"]:
                return _settings_cache["data"]
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if path == SETTINGS_PATH:
                _settings_cache["mtime"] = mtime
                _settings_cache["data"] = data
            return data
        except Exception:
            return {}


def get_trakt_token(settings_path: Path | None = None) -> str:
    data = load_plex_debrid_settings(settings_path)
    users = data.get("Trakt users") or []
    if users and isinstance(users[0], (list, tuple)) and len(users[0]) >= 2:
        return str(users[0][1] or "").strip()
    return ""


def get_trakt_username(settings_path: Path | None = None) -> str:
    data = load_plex_debrid_settings(settings_path)
    users = data.get("Trakt users") or []
    if users and isinstance(users[0], (list, tuple)) and len(users[0]) >= 1:
        return str(users[0][0] or "mrkm32").strip()
    return "mrkm32"


def get_alldebrid_key(settings_path: Path | None = None) -> str:
    data = load_plex_debrid_settings(settings_path)
    return str(data.get("All Debrid API Key") or "").strip()


def detect_lan_info() -> dict[str, str]:
    """Detect mDNS .local hostname and primary LAN IPv4 address."""
    local_host = "Marks-Mac-mini.local"
    try:
        res = subprocess.run(
            ["scutil", "--get", "LocalHostName"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        name = res.stdout.strip()
        if res.returncode == 0 and name:
            local_host = f"{name}.local"
    except Exception:
        pass

    lan_ip = ""
    for iface in ("en0", "en1"):
        try:
            res = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True,
                text=True,
                timeout=2,
            )
            ip = res.stdout.strip()
            if res.returncode == 0 and ip:
                lan_ip = ip
                break
        except Exception:
            pass

    if not lan_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.1.1", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except Exception:
            lan_ip = "127.0.0.1"

    return {
        "local_host": local_host,
        "lan_ip": lan_ip,
        "local_url": f"http://{local_host}:{PORT}",
        "ip_url": f"http://{lan_ip}:{PORT}",
    }


def _default_recommender_config() -> dict[str, Any]:
    return {
        "ai_provider": "gemini",
        "ai_model": AI_MODEL_PRESETS["gemini"]["default"],
        "ai_api_key": "",
        "auto_send_matches": True,
        "imessage_on_match": False,
        "taste_notes": {
            "mark": "",
            "shelly": "",
            "together": "",
        },
        "lan_host": "",
    }


def load_recommender_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or RECOMMENDER_CONFIG_PATH
    base = _default_recommender_config()
    with _config_lock:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if k == "taste_notes" and isinstance(v, dict):
                            base["taste_notes"].update(
                                {
                                    "mark": str(v.get("mark") or ""),
                                    "shelly": str(v.get("shelly") or ""),
                                    "together": str(v.get("together") or ""),
                                }
                            )
                        elif k in base:
                            base[k] = v
            except Exception:
                pass

    provider = str(base.get("ai_provider") or "gemini").lower()
    if provider not in AI_MODEL_PRESETS:
        provider = "gemini"
    base["ai_provider"] = provider
    if not base.get("ai_model"):
        base["ai_model"] = AI_MODEL_PRESETS[provider]["default"]
    return base


def save_recommender_config(
    updates: dict[str, Any],
    config_path: Path | None = None,
) -> dict[str, Any]:
    path = config_path or RECOMMENDER_CONFIG_PATH
    current = load_recommender_config(path)

    with _config_lock:
        if "ai_provider" in updates:
            provider = str(updates["ai_provider"] or "gemini").lower()
            if provider in AI_MODEL_PRESETS:
                current["ai_provider"] = provider
        if "ai_model" in updates and str(updates["ai_model"] or "").strip():
            current["ai_model"] = str(updates["ai_model"]).strip()
        elif "ai_provider" in updates and "ai_model" not in updates:
            current["ai_model"] = AI_MODEL_PRESETS[current["ai_provider"]]["default"]

        if "ai_api_key" in updates:
            new_key = str(updates["ai_api_key"] or "").strip()
            if new_key:
                current["ai_api_key"] = new_key
        if updates.get("clear_api_key"):
            current["ai_api_key"] = ""

        if "auto_send_matches" in updates:
            current["auto_send_matches"] = bool(updates["auto_send_matches"])
        if "imessage_on_match" in updates:
            current["imessage_on_match"] = bool(updates["imessage_on_match"])
        if "lan_host" in updates:
            current["lan_host"] = str(updates["lan_host"] or "").strip()

        if "taste_notes" in updates and isinstance(updates["taste_notes"], dict):
            notes = dict(current.get("taste_notes") or {})
            for person in ("mark", "shelly", "together"):
                if person in updates["taste_notes"]:
                    notes[person] = str(updates["taste_notes"][person] or "")
            current["taste_notes"] = notes

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

    return current


def mask_api_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 6:
        return "••••"
    return f"...{key[-4:]}"


def public_config(config: dict[str, Any] | None = None, config_path: Path | None = None) -> dict[str, Any]:
    """Return frontend-safe config with the AI API key masked."""
    cfg = config if config is not None else load_recommender_config(config_path)
    api_key = str(cfg.get("ai_api_key") or "").strip()
    lan = detect_lan_info()
    if cfg.get("lan_host"):
        lan["local_host"] = cfg["lan_host"]
        lan["local_url"] = f"http://{cfg['lan_host']}:{PORT}"
    return {
        "ai_provider": cfg.get("ai_provider", "gemini"),
        "ai_model": cfg.get("ai_model", AI_MODEL_PRESETS["gemini"]["default"]),
        "has_api_key": bool(api_key),
        "api_key_hint": mask_api_key(api_key),
        "auto_send_matches": bool(cfg.get("auto_send_matches", True)),
        "imessage_on_match": bool(cfg.get("imessage_on_match", False)),
        "taste_notes": dict(cfg.get("taste_notes") or {"mark": "", "shelly": "", "together": ""}),
        "model_presets": AI_MODEL_PRESETS,
        "lan": lan,
    }
