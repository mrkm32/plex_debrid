"""AllDebrid status verification helper for WatchNight."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

from webapp.config import get_alldebrid_key


def normalize_release_title(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[\'\":,\.\_\-\(\)\[\]]", " ", s)
    s = re.sub(r"\b(4|four)\b", "4", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_magnet_info(m: dict[str, Any]) -> dict[str, Any]:
    filename = m.get("filename") or ""
    raw = filename
    fn_no_ext = re.sub(r"\.(mkv|mp4|avi|m4v|ts)$", "", raw, flags=re.I)

    ep_range = re.search(r"\bs(\d{1,2})e(\d{1,2})[-–]e?(\d{1,2})\b", raw, re.I)
    if ep_range:
        return {
            "type": "show",
            "title": normalize_release_title(raw[: ep_range.start()]),
            "raw": filename,
        }

    multi_season = re.search(r"\bs(\d{1,2})\s*[-–to]\s*s?(\d{1,2})\b", raw, re.I)
    if multi_season:
        return {
            "type": "show",
            "title": normalize_release_title(raw[: multi_season.start()]),
            "raw": filename,
        }

    ep_match = re.search(r"\bs(\d{1,2})e(\d{1,2})\b", raw, re.I)
    if ep_match:
        return {
            "type": "show",
            "title": normalize_release_title(raw[: ep_match.start()]),
            "raw": filename,
        }

    s_pack = re.search(r"\b(?:s|season\s*)(\d{1,2})\b", raw, re.I)
    if s_pack:
        return {
            "type": "show",
            "title": normalize_release_title(raw[: s_pack.start()]),
            "raw": filename,
        }

    norm = normalize_release_title(fn_no_ext)
    year_match = re.search(r"\b(19\d\d|20\d\d)\b", norm)
    year = int(year_match.group(1)) if year_match else None
    title_part = norm[: year_match.start()].strip() if year_match else norm
    return {
        "type": "movie",
        "title": title_part,
        "year": year,
        "raw": filename,
    }


def title_matches(target_title: str, magnet_title: str) -> bool:
    t = normalize_release_title(target_title)
    m = normalize_release_title(magnet_title)
    if not t or not m:
        return False
    if t == m or t in m or m in t:
        return True
    t_ns = t.replace(" ", "")
    m_ns = m.replace(" ", "")
    return bool(t_ns and m_ns and (t_ns in m_ns or m_ns in t_ns))


class AllDebridClient:
    """Short-cached AllDebrid magnet checker to detect newly added streams quickly."""

    def __init__(self, settings_path: Path | None = None) -> None:
        self.settings_path = settings_path
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {"ts": 0.0, "magnets": []}

    def get_ready_magnets(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        apikey = get_alldebrid_key(self.settings_path)
        if not apikey:
            return []
        now = time.time()
        with self._lock:
            if not force_refresh and (now - self._cache["ts"] < 15.0):
                return list(self._cache["magnets"])

        url = f"https://api.alldebrid.com/v4.1/magnet/status?agent=plex_debrid&apikey={apikey}"
        try:
            resp = requests.get(url, timeout=15)
            data = resp.json()
            if data.get("status") == "success":
                magnets = data.get("data", {}).get("magnets", [])
                ready = [
                    parse_magnet_info(m)
                    for m in magnets
                    if m.get("statusCode") == 4 or m.get("status") == "Ready"
                ]
                with self._lock:
                    self._cache = {"ts": time.time(), "magnets": ready}
                return ready
        except Exception:
            pass
        return []

    def is_title_ready(self, title: str, media_type: str, year: int | None = None) -> bool:
        magnets = self.get_ready_magnets()
        for m in magnets:
            # Show packs without season tag in root name sometimes parse as 'movie' in heuristic;
            # allow matching on title for shows, and enforce year match when available for movies.
            if media_type == "movie" and m["type"] != "movie":
                continue
            if not title_matches(title, m.get("title") or ""):
                continue
            if media_type == "movie" and year and m.get("year"):
                if abs(int(year) - int(m["year"])) > 1:
                    continue
            return True
        return False
