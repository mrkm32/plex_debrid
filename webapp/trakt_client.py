"""Trakt API v2 client for WatchNight."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import requests

from webapp.config import TRAKT_CLIENT_ID, get_trakt_token
from webapp.db import WatchNightDB, normalize_title

TRAKT_BASE = "https://api.trakt.tv"
USER_AGENT = "WatchNight/1.0 (plex_debrid)"


def _ensure_https(url: str | None) -> str:
    if not url:
        return ""
    u = str(url).strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return f"https://{u.lstrip('/')}"


def normalize_media_item(raw_obj: dict[str, Any], media_type: str) -> dict[str, Any]:
    """Normalize a Trakt movie or show object into our media_items schema."""
    ids = raw_obj.get("ids") or {}
    images = raw_obj.get("images") or {}
    poster_list = images.get("poster") or []
    fanart_list = images.get("fanart") or images.get("thumb") or []

    poster_url = _ensure_https(poster_list[0]) if poster_list else ""
    fanart_url = _ensure_https(fanart_list[0]) if fanart_list else ""

    return {
        "trakt_id": ids.get("trakt"),
        "media_type": media_type,
        "title": raw_obj.get("title") or "",
        "year": raw_obj.get("year"),
        "imdb_id": ids.get("imdb") or "",
        "tmdb_id": ids.get("tmdb"),
        "slug": ids.get("slug") or "",
        "overview": raw_obj.get("overview") or "",
        "tagline": raw_obj.get("tagline") or "",
        "rating": raw_obj.get("rating"),
        "votes": raw_obj.get("votes"),
        "runtime": raw_obj.get("runtime"),
        "genres": raw_obj.get("genres") or [],
        "poster_url": poster_url,
        "fanart_url": fanart_url,
        "trailer_url": raw_obj.get("trailer") or "",
        "certification": raw_obj.get("certification") or "",
        "updated_at": time.time(),
    }


def _is_rate_limited(resp: requests.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        body = (resp.text or "").lower()
        return "1015" in body or "rate limited" in body
    return False


class TraktClient:
    """Thin, rate-limit-aware Trakt API v2 client with short-lived caches."""

    def __init__(self, settings_path: Path | None = None) -> None:
        self.settings_path = settings_path
        self.session = requests.Session()
        self._cache_lock = threading.Lock()
        self._browse_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._status_cache: dict[str, Any] = {"ts": 0.0, "collection": set(), "watchlist": set()}
        self._sync_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "trakt-api-key": TRAKT_CLIENT_ID,
            "trakt-api-version": "2",
        }
        token = get_trakt_token(self.settings_path)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int = 20,
        max_retry_wait: int = 15,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{TRAKT_BASE}{path}"
        last_resp: requests.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    try:
                        self.session.close()
                    except Exception:
                        pass
                    self.session = requests.Session()
                    time.sleep(0.6)
                    continue
                raise
            last_resp = resp
            if _is_rate_limited(resp) and attempt < 2:
                raw_retry = resp.headers.get("Retry-After")
                try:
                    wait_secs = max(1, min(int(raw_retry), max_retry_wait)) if raw_retry else 4
                except (TypeError, ValueError):
                    wait_secs = 4
                time.sleep(wait_secs)
                continue
            return resp
        if last_resp is not None:
            return last_resp
        assert last_exc is not None
        raise last_exc

    def _get_paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limit: int = 250,
        max_pages: int = 30,
    ) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        base_params = dict(params or {})
        page = 1
        while page <= max_pages:
            req_params = {**base_params, "page": page, "limit": limit}
            resp = self._request("GET", path, params=req_params, max_retry_wait=20)
            if resp.status_code == 401:
                raise PermissionError("Trakt returned 401 Unauthorized. Run reauth_trakt.py to refresh your token.")
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list):
                break
            all_items.extend(batch)
            raw_count = resp.headers.get("X-Pagination-Page-Count") or resp.headers.get("x-pagination-page-count")
            if not raw_count:
                break
            try:
                page_count = int(raw_count)
            except ValueError:
                break
            if page >= page_count or not batch:
                break
            page += 1
            time.sleep(0.2)
        return all_items

    def sync_trakt_to_db(self, db: WatchNightDB) -> dict[str, Any]:
        """Sync watched movies/shows, ratings, collection, and watchlist into SQLite."""
        if not self._sync_lock.acquire(blocking=False):
            return {"status": "already_running", "stats": db.get_trakt_sync_stats()}
        try:
            media_to_upsert: list[dict[str, Any]] = []
            seen_media: dict[tuple[int, str], dict[str, Any]] = {}

            def track_media(raw: dict[str, Any], mt: str) -> int | None:
                norm = normalize_media_item(raw, mt)
                tid = norm.get("trakt_id")
                if not tid:
                    return None
                seen_media[(int(tid), mt)] = norm
                return int(tid)

            # 1. Watched movies & shows
            watched_entries: list[dict[str, Any]] = []
            for item in self._get_paginated("/users/me/watched/movies"):
                movie = item.get("movie") or {}
                tid = track_media(movie, "movie")
                if tid:
                    watched_entries.append(
                        {
                            "trakt_id": tid,
                            "media_type": "movie",
                            "plays": item.get("plays") or 1,
                            "last_watched_at": item.get("last_watched_at") or "",
                        }
                    )
            for item in self._get_paginated("/users/me/watched/shows"):
                show = item.get("show") or {}
                tid = track_media(show, "show")
                if tid:
                    watched_entries.append(
                        {
                            "trakt_id": tid,
                            "media_type": "show",
                            "plays": item.get("plays") or 1,
                            "last_watched_at": item.get("last_watched_at") or "",
                        }
                    )

            # 2. Ratings (movies + shows)
            rating_entries: list[dict[str, Any]] = []
            for item in self._get_paginated("/users/me/ratings/movies"):
                movie = item.get("movie") or {}
                tid = track_media(movie, "movie")
                if tid:
                    rating_entries.append(
                        {
                            "trakt_id": tid,
                            "media_type": "movie",
                            "plays": 1,
                            "user_rating": item.get("rating"),
                            "last_watched_at": item.get("rated_at") or "",
                        }
                    )
            for item in self._get_paginated("/users/me/ratings/shows"):
                show = item.get("show") or {}
                tid = track_media(show, "show")
                if tid:
                    rating_entries.append(
                        {
                            "trakt_id": tid,
                            "media_type": "show",
                            "plays": 1,
                            "user_rating": item.get("rating"),
                            "last_watched_at": item.get("rated_at") or "",
                        }
                    )

            # 3. Collection (movies + shows)
            collection_entries: list[dict[str, Any]] = []
            for item in self._get_paginated("/sync/collection/movies"):
                movie = item.get("movie") or {}
                tid = track_media(movie, "movie")
                if tid:
                    collection_entries.append(
                        {
                            "trakt_id": tid,
                            "media_type": "movie",
                            "plays": 1,
                            "last_watched_at": item.get("collected_at") or "",
                        }
                    )
            for item in self._get_paginated("/sync/collection/shows"):
                show = item.get("show") or {}
                tid = track_media(show, "show")
                if tid:
                    collection_entries.append(
                        {
                            "trakt_id": tid,
                            "media_type": "show",
                            "plays": 1,
                            "last_watched_at": item.get("last_collected_at") or item.get("updated_at") or "",
                        }
                    )

            # 4. Watchlist
            watchlist_entries: list[dict[str, Any]] = []
            for item in self._get_paginated("/sync/watchlist"):
                mt = item.get("type")
                if mt not in ("movie", "show"):
                    continue
                obj = item.get(mt) or {}
                tid = track_media(obj, mt)
                if tid:
                    watchlist_entries.append(
                        {
                            "trakt_id": tid,
                            "media_type": mt,
                            "plays": 1,
                            "last_watched_at": item.get("listed_at") or "",
                        }
                    )

            media_to_upsert = list(seen_media.values())
            db.upsert_media_items(media_to_upsert)
            db.replace_trakt_history_source("watched", watched_entries)
            db.replace_trakt_history_source("rating", rating_entries)
            db.replace_trakt_history_source("collection", collection_entries)
            db.replace_trakt_history_source("watchlist", watchlist_entries)

            # Invalidate short status cache
            with self._cache_lock:
                self._status_cache = {
                    "ts": time.time(),
                    "collection": {(e["trakt_id"], e["media_type"]) for e in collection_entries},
                    "watchlist": {(e["trakt_id"], e["media_type"]) for e in watchlist_entries},
                }

            db.set_sync_meta("last_trakt_sync", {"ok": True, "at": time.time()})
            db.set_sync_meta("last_trakt_sync_error", "")
            return {"status": "ok", "stats": db.get_trakt_sync_stats()}
        except Exception as exc:
            db.set_sync_meta("last_trakt_sync_error", str(exc))
            raise
        finally:
            self._sync_lock.release()

    def search_trakt(
        self,
        query: str,
        media_type: str = "movie,show",
        limit: int = 12,
        db: WatchNightDB | None = None,
    ) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        resp = self._request(
            "GET",
            f"/search/{media_type}",
            params={"query": q, "extended": "full", "limit": limit},
        )
        resp.raise_for_status()
        results: list[dict[str, Any]] = []
        for entry in resp.json():
            mt = entry.get("type")
            if mt not in ("movie", "show"):
                continue
            obj = entry.get(mt) or {}
            norm = normalize_media_item(obj, mt)
            if norm.get("trakt_id"):
                results.append(norm)
        if db and results:
            db.upsert_media_items(results)
        return results

    def resolve_title(
        self,
        title: str,
        year: int | None = None,
        media_type: str | None = None,
        db: WatchNightDB | None = None,
    ) -> dict[str, Any] | None:
        """Resolve an AI-suggested title to a verified Trakt media item."""
        mt_param = media_type if media_type in ("movie", "show") else "movie,show"
        candidates = self.search_trakt(title, media_type=mt_param, limit=8, db=db)
        if not candidates and mt_param != "movie,show":
            candidates = self.search_trakt(title, media_type="movie,show", limit=8, db=db)
        if not candidates:
            return None

        target_norm = normalize_title(title)

        def score_candidate(c: dict[str, Any]) -> tuple[int, int, int, float]:
            c_norm = normalize_title(c.get("title"))
            exact_title = 2 if c_norm == target_norm else (1 if target_norm in c_norm or c_norm in target_norm else 0)
            year_score = 0
            if year and c.get("year"):
                diff = abs(int(c["year"]) - int(year))
                if diff == 0:
                    year_score = 3
                elif diff == 1:
                    year_score = 2
                elif diff <= 2:
                    year_score = 1
            type_match = 1 if (not media_type or c.get("media_type") == media_type) else 0
            votes = float(c.get("votes") or 0)
            return (exact_title, year_score, type_match, votes)

        candidates.sort(key=score_candidate, reverse=True)
        best = candidates[0]
        # Require reasonable title overlap if exact wasn't found
        best_norm = normalize_title(best.get("title"))
        if target_norm != best_norm and target_norm not in best_norm and best_norm not in target_norm:
            target_words = set(target_norm.split())
            best_words = set(best_norm.split())
            if not target_words or len(target_words & best_words) / len(target_words) < 0.5:
                return None
        return best

    def get_browse_list(
        self,
        category: str,
        limit: int = 20,
        db: WatchNightDB | None = None,
    ) -> list[dict[str, Any]]:
        endpoints = {
            "trending_shows": ("/shows/trending", "show"),
            "trending_movies": ("/movies/trending", "movie"),
            "popular_shows": ("/shows/popular", "show"),
            "popular_movies": ("/movies/popular", "movie"),
        }
        if category not in endpoints and category != "romcoms":
            raise ValueError(f"Unsupported browse category: {category}")

        now = time.time()
        with self._cache_lock:
            cached = self._browse_cache.get(category)
            if cached and (now - cached[0] < 1800):
                return cached[1][:limit]

        if category == "romcoms":
            def _fetch_romcoms(path: str, mt: str) -> list[dict[str, Any]]:
                resp = self._request(
                    "GET",
                    path,
                    params={"genres": "romance", "languages": "en", "extended": "full", "limit": 50},
                )
                resp.raise_for_status()
                filtered: list[dict[str, Any]] = []
                for entry in resp.json():
                    raw = entry.get(mt) if mt in entry else entry
                    if not isinstance(raw, dict):
                        continue
                    genres_lower = {str(g).lower() for g in (raw.get("genres") or [])}
                    if "romance" in genres_lower and "comedy" in genres_lower:
                        norm = normalize_media_item(raw, mt)
                        if norm.get("trakt_id"):
                            filtered.append(norm)
                return filtered

            movies = _fetch_romcoms("/movies/popular", "movie")
            shows = _fetch_romcoms("/shows/popular", "show")
            items = []
            m_idx = 0
            s_idx = 0
            while m_idx < len(movies) or s_idx < len(shows):
                for _ in range(3):
                    if m_idx < len(movies):
                        items.append(movies[m_idx])
                        m_idx += 1
                if s_idx < len(shows):
                    items.append(shows[s_idx])
                    s_idx += 1
        else:
            path, mt = endpoints[category]
            resp = self._request("GET", path, params={"extended": "full", "limit": max(limit, 25)})
            resp.raise_for_status()
            items = []
            for entry in resp.json():
                raw = entry.get(mt) if mt in entry else entry
                if isinstance(raw, dict):
                    norm = normalize_media_item(raw, mt)
                    if norm.get("trakt_id"):
                        items.append(norm)

        if db and items:
            db.upsert_media_items(items)
        with self._cache_lock:
            self._browse_cache[category] = (now, items)
        return items[:limit]

    def get_related(
        self,
        trakt_id: int,
        media_type: str,
        limit: int = 10,
        db: WatchNightDB | None = None,
    ) -> list[dict[str, Any]]:
        plural = "movies" if media_type == "movie" else "shows"
        resp = self._request(
            "GET",
            f"/{plural}/{int(trakt_id)}/related",
            params={"extended": "full", "limit": limit},
        )
        if resp.status_code != 200:
            return []
        items: list[dict[str, Any]] = []
        for raw in resp.json():
            if isinstance(raw, dict):
                norm = normalize_media_item(raw, media_type)
                if norm.get("trakt_id"):
                    items.append(norm)
        if db and items:
            db.upsert_media_items(items)
        return items

    def add_to_watchlist(self, trakt_id: int, media_type: str) -> dict[str, Any]:
        key = "movies" if media_type == "movie" else "shows"
        payload = {key: [{"ids": {"trakt": int(trakt_id)}}]}
        resp = self._request("POST", "/sync/watchlist", json_body=payload)
        if resp.status_code == 401:
            raise PermissionError("Trakt returned 401 Unauthorized. Run reauth_trakt.py to refresh your token.")
        resp.raise_for_status()
        with self._cache_lock:
            self._status_cache["ts"] = 0.0
        return resp.json()

    def get_collection_and_watchlist_ids(
        self,
        db: WatchNightDB | None = None,
        force_refresh: bool = False,
    ) -> tuple[set[tuple[int, str]], set[tuple[int, str]]]:
        """Short-cached (10s) live Trakt Collection + Watchlist ID sets for Infuse polling."""
        now = time.time()
        with self._cache_lock:
            if not force_refresh and (now - self._status_cache["ts"] < 10.0):
                return set(self._status_cache["collection"]), set(self._status_cache["watchlist"])

        collection: set[tuple[int, str]] = set()
        watchlist: set[tuple[int, str]] = set()
        col_entries: list[dict[str, Any]] = []

        for path, mt in (("/sync/collection/movies", "movie"), ("/sync/collection/shows", "show")):
            resp = self._request("GET", path)
            if resp.status_code == 200:
                for item in resp.json():
                    obj = item.get(mt) or {}
                    tid = (obj.get("ids") or {}).get("trakt")
                    if tid:
                        collection.add((int(tid), mt))
                        col_entries.append(
                            {
                                "trakt_id": int(tid),
                                "media_type": mt,
                                "plays": 1,
                                "last_watched_at": item.get("collected_at") or item.get("last_collected_at") or "",
                            }
                        )

        wl_resp = self._request("GET", "/sync/watchlist")
        if wl_resp.status_code == 200:
            for item in wl_resp.json():
                mt = item.get("type")
                if mt in ("movie", "show"):
                    tid = ((item.get(mt) or {}).get("ids") or {}).get("trakt")
                    if tid:
                        watchlist.add((int(tid), mt))

        if db and col_entries:
            db.replace_trakt_history_source("collection", col_entries)

        with self._cache_lock:
            self._status_cache = {
                "ts": time.time(),
                "collection": collection,
                "watchlist": watchlist,
            }
        return set(collection), set(watchlist)
