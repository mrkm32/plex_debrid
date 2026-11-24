"""SQLite persistence layer for WatchNight."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from webapp.config import DB_PATH

VALID_USERS = ("mark", "shelly")
VALID_REACTIONS = ("want", "pass", "seen_loved", "seen_ok", "seen_disliked")
VALID_MEDIA_TYPES = ("movie", "show")


def normalize_title(s: str | None) -> str:
    if not s:
        return ""
    text = s.lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"[\'\":,\.\_\-\(\)\[\]!?]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def proxy_image_url(raw_url: str | None) -> str:
    if not raw_url:
        return ""
    url = raw_url.strip()
    if not url:
        return ""
    if url.startswith("/api/image?"):
        return url
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url.lstrip('/')}"
    return f"/api/image?url={quote(url, safe='')}"


class WatchNightDB:
    """Thread-safe SQLite store with WAL enabled."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._lock = threading.RLock()
        self.init_db()

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if write:
            self._lock.acquire()
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA journal_mode=WAL;")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()
            if write:
                self._lock.release()

    def init_db(self) -> None:
        with self._connect(write=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS media_items (
                    trakt_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    imdb_id TEXT,
                    tmdb_id INTEGER,
                    slug TEXT,
                    overview TEXT,
                    tagline TEXT,
                    rating REAL,
                    votes INTEGER,
                    runtime INTEGER,
                    genres_json TEXT,
                    poster_url TEXT,
                    fanart_url TEXT,
                    trailer_url TEXT,
                    certification TEXT,
                    updated_at REAL,
                    PRIMARY KEY (trakt_id, media_type)
                );

                CREATE TABLE IF NOT EXISTS trakt_history (
                    trakt_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    plays INTEGER DEFAULT 1,
                    user_rating INTEGER,
                    last_watched_at TEXT,
                    synced_at REAL,
                    PRIMARY KEY (trakt_id, media_type, source)
                );

                CREATE TABLE IF NOT EXISTS reactions (
                    user TEXT NOT NULL,
                    trakt_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    reaction TEXT NOT NULL,
                    updated_at REAL,
                    PRIMARY KEY (user, trakt_id, media_type)
                );

                CREATE TABLE IF NOT EXISTS netflix_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user TEXT NOT NULL,
                    base_title TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    trakt_id INTEGER,
                    play_count INTEGER DEFAULT 1,
                    last_watched TEXT,
                    imported_at REAL,
                    UNIQUE(user, base_title, media_type)
                );

                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    mood_prompt TEXT,
                    trakt_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    pitch TEXT,
                    tags_json TEXT,
                    source TEXT,
                    score REAL,
                    batch_id TEXT,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS infuse_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trakt_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    requested_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_detail TEXT,
                    requested_at REAL,
                    ready_at REAL,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS sync_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_trakt_history_source ON trakt_history(source);
                CREATE INDEX IF NOT EXISTS idx_reactions_user ON reactions(user, reaction);
                CREATE INDEX IF NOT EXISTS idx_recs_mode_created ON recommendations(mode, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_infuse_status ON infuse_requests(status, requested_at DESC);
                """
            )

    def upsert_media_items(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        now = time.time()
        rows = []
        for item in items:
            trakt_id = item.get("trakt_id")
            media_type = item.get("media_type")
            title = item.get("title")
            if not trakt_id or media_type not in VALID_MEDIA_TYPES or not title:
                continue
            genres = item.get("genres")
            if isinstance(genres, str):
                genres_json = genres
            else:
                genres_json = json.dumps(list(genres or []))
            rows.append(
                (
                    int(trakt_id),
                    media_type,
                    str(title),
                    item.get("year"),
                    item.get("imdb_id") or "",
                    item.get("tmdb_id"),
                    item.get("slug") or "",
                    item.get("overview") or "",
                    item.get("tagline") or "",
                    item.get("rating"),
                    item.get("votes"),
                    item.get("runtime"),
                    genres_json,
                    item.get("poster_url") or "",
                    item.get("fanart_url") or "",
                    item.get("trailer_url") or "",
                    item.get("certification") or "",
                    float(item.get("updated_at") or now),
                )
            )
        if not rows:
            return

        sql = """
        INSERT INTO media_items (
            trakt_id, media_type, title, year, imdb_id, tmdb_id, slug,
            overview, tagline, rating, votes, runtime, genres_json,
            poster_url, fanart_url, trailer_url, certification, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trakt_id, media_type) DO UPDATE SET
            title = COALESCE(NULLIF(excluded.title, ''), media_items.title),
            year = COALESCE(excluded.year, media_items.year),
            imdb_id = COALESCE(NULLIF(excluded.imdb_id, ''), media_items.imdb_id),
            tmdb_id = COALESCE(excluded.tmdb_id, media_items.tmdb_id),
            slug = COALESCE(NULLIF(excluded.slug, ''), media_items.slug),
            overview = COALESCE(NULLIF(excluded.overview, ''), media_items.overview),
            tagline = COALESCE(NULLIF(excluded.tagline, ''), media_items.tagline),
            rating = COALESCE(excluded.rating, media_items.rating),
            votes = COALESCE(excluded.votes, media_items.votes),
            runtime = COALESCE(excluded.runtime, media_items.runtime),
            genres_json = CASE
                WHEN excluded.genres_json IS NOT NULL AND excluded.genres_json != '[]' AND excluded.genres_json != ''
                THEN excluded.genres_json
                ELSE media_items.genres_json
            END,
            poster_url = COALESCE(NULLIF(excluded.poster_url, ''), media_items.poster_url),
            fanart_url = COALESCE(NULLIF(excluded.fanart_url, ''), media_items.fanart_url),
            trailer_url = COALESCE(NULLIF(excluded.trailer_url, ''), media_items.trailer_url),
            certification = COALESCE(NULLIF(excluded.certification, ''), media_items.certification),
            updated_at = MAX(COALESCE(media_items.updated_at, 0), excluded.updated_at)
        """
        with self._connect(write=True) as conn:
            conn.executemany(sql, rows)

    def upsert_media_item(self, item: dict[str, Any]) -> None:
        self.upsert_media_items([item])

    @staticmethod
    def row_to_media_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        genres_raw = d.pop("genres_json", None)
        if genres_raw:
            try:
                d["genres"] = json.loads(genres_raw)
            except Exception:
                d["genres"] = []
        elif "genres" not in d:
            d["genres"] = []

        tags_raw = d.pop("tags_json", None)
        if tags_raw is not None:
            try:
                d["tags"] = json.loads(tags_raw)
            except Exception:
                d["tags"] = []

        d["poster_proxy"] = proxy_image_url(d.get("poster_url"))
        d["fanart_proxy"] = proxy_image_url(d.get("fanart_url"))
        return d

    def get_media_item(self, trakt_id: int, media_type: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_items WHERE trakt_id = ? AND media_type = ?",
                (int(trakt_id), media_type),
            ).fetchone()
        return self.row_to_media_dict(row) if row else None

    def replace_trakt_history_source(self, source: str, entries: list[dict[str, Any]]) -> int:
        now = time.time()
        rows = []
        for e in entries:
            tid = e.get("trakt_id")
            mt = e.get("media_type")
            if not tid or mt not in VALID_MEDIA_TYPES:
                continue
            rows.append(
                (
                    int(tid),
                    mt,
                    source,
                    int(e.get("plays") or 1),
                    e.get("user_rating"),
                    e.get("last_watched_at") or "",
                    now,
                )
            )
        with self._connect(write=True) as conn:
            conn.execute("DELETE FROM trakt_history WHERE source = ?", (source,))
            if rows:
                conn.executemany(
                    """
                    INSERT INTO trakt_history (
                        trakt_id, media_type, source, plays, user_rating, last_watched_at, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trakt_id, media_type, source) DO UPDATE SET
                        plays = excluded.plays,
                        user_rating = COALESCE(excluded.user_rating, trakt_history.user_rating),
                        last_watched_at = COALESCE(NULLIF(excluded.last_watched_at, ''), trakt_history.last_watched_at),
                        synced_at = excluded.synced_at
                    """,
                    rows,
                )
        return len(rows)

    def set_sync_meta(self, key: str, value: Any) -> None:
        now = time.time()
        val_str = value if isinstance(value, str) else json.dumps(value)
        with self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO sync_meta (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, val_str, now),
            )

    def get_sync_meta(self, key: str) -> tuple[str | None, float | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM sync_meta WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None, None
        return row["value"], row["updated_at"]

    def get_trakt_sync_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            counts = {}
            for src in ("watched", "collection", "watchlist", "rating"):
                c_movie = conn.execute(
                    "SELECT COUNT(*) FROM trakt_history WHERE source = ? AND media_type = 'movie'",
                    (src,),
                ).fetchone()[0]
                c_show = conn.execute(
                    "SELECT COUNT(*) FROM trakt_history WHERE source = ? AND media_type = 'show'",
                    (src,),
                ).fetchone()[0]
                counts[src] = {"movies": c_movie, "shows": c_show, "total": c_movie + c_show}
            netflix_cnt = conn.execute("SELECT COUNT(*) FROM netflix_history").fetchone()[0]
            meta_row = conn.execute(
                "SELECT value, updated_at FROM sync_meta WHERE key = 'last_trakt_sync'"
            ).fetchone()
        last_sync = meta_row["updated_at"] if meta_row else None
        last_error_val, last_error_at = self.get_sync_meta("last_trakt_sync_error")
        return {
            "last_synced_at": last_sync,
            "last_error": last_error_val,
            "last_error_at": last_error_at,
            "counts": counts,
            "netflix_count": netflix_cnt,
        }

    def set_reaction(self, user: str, trakt_id: int, media_type: str, reaction: str) -> None:
        if user not in VALID_USERS:
            raise ValueError(f"Invalid user: {user}")
        if media_type not in VALID_MEDIA_TYPES:
            raise ValueError(f"Invalid media_type: {media_type}")
        if reaction not in VALID_REACTIONS:
            raise ValueError(f"Invalid reaction: {reaction}")
        now = time.time()
        with self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO reactions (user, trakt_id, media_type, reaction, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user, trakt_id, media_type) DO UPDATE SET
                    reaction = excluded.reaction,
                    updated_at = excluded.updated_at
                """,
                (user, int(trakt_id), media_type, reaction, now),
            )

    def delete_reaction(self, user: str, trakt_id: int, media_type: str) -> None:
        with self._connect(write=True) as conn:
            conn.execute(
                "DELETE FROM reactions WHERE user = ? AND trakt_id = ? AND media_type = ?",
                (user, int(trakt_id), media_type),
            )

    def get_user_reactions(self, user: str) -> dict[tuple[int, str], str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trakt_id, media_type, reaction FROM reactions WHERE user = ?",
                (user,),
            ).fetchall()
        return {(int(r["trakt_id"]), r["media_type"]): r["reaction"] for r in rows}

    def get_user_reaction_counts(self, user: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT reaction, COUNT(*) AS cnt FROM reactions WHERE user = ? GROUP BY reaction",
                (user,),
            ).fetchall()
            nf_cnt = conn.execute(
                "SELECT COUNT(*) FROM netflix_history WHERE user = ?",
                (user,),
            ).fetchone()[0]
        counts = {r["reaction"]: int(r["cnt"]) for r in rows}
        counts["total_reactions"] = sum(counts.values())
        counts["positives"] = counts.get("want", 0) + counts.get("seen_loved", 0)
        counts["netflix_items"] = int(nf_cnt)
        return counts

    def get_partner(self, user: str) -> str:
        return "shelly" if user == "mark" else "mark"

    def get_partner_want_set(self, user: str) -> set[tuple[int, str]]:
        partner = self.get_partner(user)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trakt_id, media_type FROM reactions WHERE user = ? AND reaction = 'want'",
                (partner,),
            ).fetchall()
        return {(int(r["trakt_id"]), r["media_type"]) for r in rows}

    def check_mutual_match(self, trakt_id: int, media_type: str) -> bool:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user FROM reactions
                WHERE trakt_id = ? AND media_type = ? AND reaction = 'want'
                """,
                (int(trakt_id), media_type),
            ).fetchall()
        users = {r["user"] for r in rows}
        return "mark" in users and "shelly" in users

    def get_mutual_matches(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*, MAX(r1.updated_at, r2.updated_at) AS matched_at
                FROM reactions r1
                JOIN reactions r2
                  ON r1.trakt_id = r2.trakt_id
                 AND r1.media_type = r2.media_type
                 AND r1.user = 'mark'
                 AND r2.user = 'shelly'
                 AND r1.reaction = 'want'
                 AND r2.reaction = 'want'
                JOIN media_items m
                  ON m.trakt_id = r1.trakt_id
                 AND m.media_type = r1.media_type
                ORDER BY matched_at DESC
                """
            ).fetchall()
        status_map = self.get_latest_infuse_status_map()
        collection_set = self.get_trakt_source_set("collection")
        out = []
        for row in rows:
            item = self.row_to_media_dict(row)
            key = (int(item["trakt_id"]), item["media_type"])
            req = status_map.get(key)
            if key in collection_set:
                item["infuse_status"] = "ready"
                item["in_infuse"] = True
            elif req:
                item["infuse_status"] = req["status"]
                item["in_infuse"] = req["status"] == "ready"
            else:
                item["infuse_status"] = None
                item["in_infuse"] = False
            out.append(item)
        return out

    def get_trakt_source_set(self, source: str) -> set[tuple[int, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trakt_id, media_type FROM trakt_history WHERE source = ?",
                (source,),
            ).fetchall()
        return {(int(r["trakt_id"]), r["media_type"]) for r in rows}

    def get_exclusion_sets(
        self,
        mode: str = "together",
        user: str = "shelly",
    ) -> tuple[set[tuple[int, str]], set[str]]:
        """Return (excluded_keys, excluded_normalized_titles) for recommendation filtering."""
        excluded_keys: set[tuple[int, str]] = set()
        excluded_titles: set[str] = set()
        partner = self.get_partner(user)

        with self._connect() as conn:
            # 1. Trakt watched, collection, watchlist
            rows = conn.execute(
                """
                SELECT th.trakt_id, th.media_type, m.title
                FROM trakt_history th
                LEFT JOIN media_items m
                  ON m.trakt_id = th.trakt_id AND m.media_type = th.media_type
                WHERE th.source IN ('watched', 'collection', 'watchlist')
                """
            ).fetchall()
            for r in rows:
                excluded_keys.add((int(r["trakt_id"]), r["media_type"]))
                if r["title"]:
                    excluded_titles.add(normalize_title(r["title"]))

            # 2. Active/ready Infuse requests
            req_rows = conn.execute(
                "SELECT trakt_id, media_type, title FROM infuse_requests WHERE status IN ('queued', 'searching', 'ready')"
            ).fetchall()
            for r in req_rows:
                excluded_keys.add((int(r["trakt_id"]), r["media_type"]))
                if r["title"]:
                    excluded_titles.add(normalize_title(r["title"]))

            # 3. Netflix history
            if mode == "together":
                nf_rows = conn.execute("SELECT base_title, trakt_id, media_type FROM netflix_history").fetchall()
            else:
                nf_rows = conn.execute(
                    "SELECT base_title, trakt_id, media_type FROM netflix_history WHERE user = ?",
                    (user,),
                ).fetchall()
            for r in nf_rows:
                if r["trakt_id"] and r["media_type"]:
                    excluded_keys.add((int(r["trakt_id"]), r["media_type"]))
                if r["base_title"]:
                    excluded_titles.add(normalize_title(r["base_title"]))

            # 4. In-app reactions
            # Active user should never re-see anything they already reacted to.
            user_reacts = conn.execute(
                """
                SELECT r.trakt_id, r.media_type, m.title
                FROM reactions r
                LEFT JOIN media_items m
                  ON m.trakt_id = r.trakt_id AND m.media_type = r.media_type
                WHERE r.user = ?
                """,
                (user,),
            ).fetchall()
            for r in user_reacts:
                excluded_keys.add((int(r["trakt_id"]), r["media_type"]))
                if r["title"]:
                    excluded_titles.add(normalize_title(r["title"]))

            # In "together" mode, also exclude anything the partner passed or has already seen
            # (but KEEP partner's 'want' items so the current user can match on them!).
            if mode == "together":
                partner_blocks = conn.execute(
                    """
                    SELECT r.trakt_id, r.media_type, m.title
                    FROM reactions r
                    LEFT JOIN media_items m
                      ON m.trakt_id = r.trakt_id AND m.media_type = r.media_type
                    WHERE r.user = ? AND r.reaction IN ('pass', 'seen_loved', 'seen_ok', 'seen_disliked')
                    """,
                    (partner,),
                ).fetchall()
                for r in partner_blocks:
                    excluded_keys.add((int(r["trakt_id"]), r["media_type"]))
                    if r["title"]:
                        excluded_titles.add(normalize_title(r["title"]))

        excluded_titles.discard("")
        return excluded_keys, excluded_titles

    def save_recommendations(
        self,
        mode: str,
        mood_prompt: str,
        items: list[dict[str, Any]],
        source: str,
        batch_id: str,
        clear_previous_mode: bool = True,
    ) -> None:
        now = time.time()
        with self._connect(write=True) as conn:
            if clear_previous_mode:
                conn.execute("DELETE FROM recommendations WHERE mode = ?", (mode,))
            rows = []
            for idx, item in enumerate(items):
                tid = item.get("trakt_id")
                mt = item.get("media_type")
                if not tid or mt not in VALID_MEDIA_TYPES:
                    continue
                tags = item.get("tags") or []
                rows.append(
                    (
                        mode,
                        mood_prompt or "",
                        int(tid),
                        mt,
                        item.get("pitch") or "",
                        json.dumps(list(tags)),
                        source,
                        float(item.get("score") if item.get("score") is not None else (100 - idx)),
                        batch_id,
                        now,
                    )
                )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO recommendations (
                        mode, mood_prompt, trakt_id, media_type, pitch, tags_json,
                        source, score, batch_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def get_deck(
        self,
        mode: str = "together",
        user: str = "shelly",
        media_type: str = "both",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        excluded_keys, excluded_titles = self.get_exclusion_sets(mode=mode, user=user)
        partner_wants = self.get_partner_want_set(user)
        partner_name = "Shelly" if self.get_partner(user) == "shelly" else "Mark"
        status_map = self.get_latest_infuse_status_map()

        with self._connect() as conn:
            params: list[Any] = [mode]
            type_clause = ""
            if media_type in VALID_MEDIA_TYPES:
                type_clause = "AND r.media_type = ?"
                params.append(media_type)
            rows = conn.execute(
                f"""
                SELECT r.id AS rec_id, r.mode, r.mood_prompt, r.pitch, r.tags_json, r.source AS rec_source,
                       r.score, r.batch_id, r.created_at, m.*
                FROM recommendations r
                JOIN media_items m
                  ON m.trakt_id = r.trakt_id AND m.media_type = r.media_type
                WHERE r.mode = ? {type_clause}
                ORDER BY r.created_at DESC, r.score DESC, r.id ASC
                """,
                params,
            ).fetchall()

        seen_keys: set[tuple[int, str]] = set()
        cards: list[dict[str, Any]] = []

        # Also surface partner's "want" picks at the top of the Together deck if the current user hasn't reacted yet
        if mode == "together" and partner_wants:
            with self._connect() as conn:
                for p_tid, p_mt in partner_wants:
                    if media_type in VALID_MEDIA_TYPES and p_mt != media_type:
                        continue
                    if (p_tid, p_mt) in excluded_keys:
                        continue
                    mrow = conn.execute(
                        "SELECT * FROM media_items WHERE trakt_id = ? AND media_type = ?",
                        (p_tid, p_mt),
                    ).fetchone()
                    if not mrow:
                        continue
                    item = self.row_to_media_dict(mrow)
                    if normalize_title(item.get("title")) in excluded_titles:
                        continue
                    seen_keys.add((p_tid, p_mt))
                    item["pitch"] = item.get("pitch") or f"{partner_name} already tapped thumbs-up on this one — tap Want to match and send it to Infuse!"
                    item["tags"] = item.get("tags") or ["Partner Pick"]
                    item["rec_source"] = "partner_want"
                    item["partner_wanted"] = True
                    item["partner_name"] = partner_name
                    req = status_map.get((p_tid, p_mt))
                    item["infuse_status"] = req["status"] if req else None
                    item["in_infuse"] = bool(req and req["status"] == "ready")
                    cards.append(item)

        for row in rows:
            key = (int(row["trakt_id"]), row["media_type"])
            if key in seen_keys or key in excluded_keys:
                continue
            item = self.row_to_media_dict(row)
            if normalize_title(item.get("title")) in excluded_titles:
                continue
            seen_keys.add(key)
            item["partner_wanted"] = key in partner_wants
            item["partner_name"] = partner_name
            req = status_map.get(key)
            item["infuse_status"] = req["status"] if req else None
            item["in_infuse"] = bool(req and req["status"] == "ready")
            cards.append(item)
            if len(cards) >= limit:
                break

        return cards[:limit]

    def upsert_netflix_history(self, user: str, entries: list[dict[str, Any]]) -> tuple[int, int]:
        if user not in VALID_USERS:
            raise ValueError(f"Invalid user: {user}")
        now = time.time()
        shows = 0
        movies = 0
        rows = []
        for e in entries:
            title = str(e.get("base_title") or "").strip()
            mt = e.get("media_type")
            if not title or mt not in VALID_MEDIA_TYPES:
                continue
            if mt == "show":
                shows += 1
            else:
                movies += 1
            rows.append(
                (
                    user,
                    title,
                    mt,
                    e.get("trakt_id"),
                    int(e.get("play_count") or 1),
                    str(e.get("last_watched") or ""),
                    now,
                )
            )
        if rows:
            with self._connect(write=True) as conn:
                conn.executemany(
                    """
                    INSERT INTO netflix_history (
                        user, base_title, media_type, trakt_id, play_count, last_watched, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user, base_title, media_type) DO UPDATE SET
                        play_count = MAX(netflix_history.play_count, excluded.play_count),
                        last_watched = COALESCE(NULLIF(excluded.last_watched, ''), netflix_history.last_watched),
                        trakt_id = COALESCE(excluded.trakt_id, netflix_history.trakt_id),
                        imported_at = excluded.imported_at
                    """,
                    rows,
                )
        return shows, movies

    def get_unresolved_netflix_titles(self, user: str, limit: int = 40) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user, base_title, media_type, play_count, last_watched
                FROM netflix_history
                WHERE user = ? AND trakt_id IS NULL
                ORDER BY play_count DESC, last_watched DESC
                LIMIT ?
                """,
                (user, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_netflix_trakt_id(self, record_id: int, trakt_id: int) -> None:
        with self._connect(write=True) as conn:
            conn.execute(
                "UPDATE netflix_history SET trakt_id = ? WHERE id = ?",
                (int(trakt_id), int(record_id)),
            )

    def create_infuse_request(
        self,
        trakt_id: int,
        media_type: str,
        title: str,
        year: int | None,
        requested_by: str,
        status: str = "queued",
        status_detail: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        ready_at = now if status == "ready" else None
        with self._connect(write=True) as conn:
            existing = conn.execute(
                """
                SELECT * FROM infuse_requests
                WHERE trakt_id = ? AND media_type = ? AND status IN ('queued', 'searching', 'ready')
                ORDER BY id DESC LIMIT 1
                """,
                (int(trakt_id), media_type),
            ).fetchone()
            if existing:
                return dict(existing)

            cur = conn.execute(
                """
                INSERT INTO infuse_requests (
                    trakt_id, media_type, title, year, requested_by, status,
                    status_detail, requested_at, ready_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(trakt_id),
                    media_type,
                    title,
                    year,
                    requested_by,
                    status,
                    status_detail,
                    now,
                    ready_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM infuse_requests WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return dict(row)

    def update_infuse_request_status(
        self,
        request_id: int,
        status: str,
        status_detail: str = "",
    ) -> None:
        now = time.time()
        with self._connect(write=True) as conn:
            if status == "ready":
                conn.execute(
                    """
                    UPDATE infuse_requests
                    SET status = ?, status_detail = ?, ready_at = COALESCE(ready_at, ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (status, status_detail, now, now, int(request_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE infuse_requests
                    SET status = ?, status_detail = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, status_detail, now, int(request_id)),
                )

    def get_pending_infuse_requests(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM infuse_requests
                WHERE status IN ('queued', 'searching')
                ORDER BY requested_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def get_infuse_requests(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ir.*, m.poster_url, m.rating, m.runtime, m.genres_json
                FROM infuse_requests ir
                LEFT JOIN media_items m
                  ON m.trakt_id = ir.trakt_id AND m.media_type = ir.media_type
                ORDER BY ir.requested_at DESC, ir.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self.row_to_media_dict(r) for r in rows]

    def get_latest_infuse_status_map(self) -> dict[tuple[int, str], dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM infuse_requests
                ORDER BY id ASC
                """
            ).fetchall()
        out: dict[tuple[int, str], dict[str, Any]] = {}
        for r in rows:
            out[(int(r["trakt_id"]), r["media_type"])] = dict(r)
        return out

    def get_user_taste_details(self, user: str) -> dict[str, Any]:
        with self._connect() as conn:
            react_rows = conn.execute(
                """
                SELECT r.reaction, r.updated_at, m.*
                FROM reactions r
                JOIN media_items m
                  ON m.trakt_id = r.trakt_id AND m.media_type = r.media_type
                WHERE r.user = ?
                ORDER BY r.updated_at DESC
                """,
                (user,),
            ).fetchall()
            nf_rows = conn.execute(
                """
                SELECT id, base_title, media_type, trakt_id, play_count, last_watched
                FROM netflix_history
                WHERE user = ?
                ORDER BY play_count DESC, last_watched DESC
                LIMIT 60
                """,
                (user,),
            ).fetchall()

        loved, wanted, seen_other, passed = [], [], [], []
        for row in react_rows:
            item = self.row_to_media_dict(row)
            react = item["reaction"]
            if react == "seen_loved":
                loved.append(item)
            elif react == "want":
                wanted.append(item)
            elif react == "pass":
                passed.append(item)
            else:
                seen_other.append(item)

        return {
            "loved": loved,
            "wanted": wanted,
            "seen_other": seen_other,
            "passed": passed,
            "netflix_top": [dict(r) for r in nf_rows],
        }

    def get_taste_summary_for_ai(self) -> dict[str, Any]:
        """Collect concise Mark, Shelly, Together, and exclusion summaries for AI prompting."""
        with self._connect() as conn:
            # Mark's top-rated Trakt titles (8-10)
            mark_rated = conn.execute(
                """
                SELECT m.title, m.year, th.media_type, th.user_rating, m.genres_json
                FROM trakt_history th
                JOIN media_items m
                  ON m.trakt_id = th.trakt_id AND m.media_type = th.media_type
                WHERE th.source = 'rating' AND th.user_rating >= 8
                ORDER BY th.user_rating DESC, th.last_watched_at DESC
                LIMIT 35
                """
            ).fetchall()

            # Mark's low-rated Trakt titles (1-5) as negative signal
            mark_disliked_trakt = conn.execute(
                """
                SELECT m.title, m.year, th.media_type, th.user_rating
                FROM trakt_history th
                JOIN media_items m
                  ON m.trakt_id = th.trakt_id AND m.media_type = th.media_type
                WHERE th.source = 'rating' AND th.user_rating <= 5
                ORDER BY th.last_watched_at DESC
                LIMIT 15
                """
            ).fetchall()

            # Mark's highest-play Trakt shows
            mark_top_shows = conn.execute(
                """
                SELECT m.title, m.year, th.plays, th.last_watched_at
                FROM trakt_history th
                JOIN media_items m
                  ON m.trakt_id = th.trakt_id AND m.media_type = th.media_type
                WHERE th.source = 'watched' AND th.media_type = 'show' AND th.plays >= 4
                ORDER BY th.plays DESC, th.last_watched_at DESC
                LIMIT 25
                """
            ).fetchall()

            # Recent Trakt watches (movies + shows)
            mark_recent = conn.execute(
                """
                SELECT m.title, m.year, th.media_type, th.last_watched_at
                FROM trakt_history th
                JOIN media_items m
                  ON m.trakt_id = th.trakt_id AND m.media_type = th.media_type
                WHERE th.source = 'watched'
                ORDER BY th.last_watched_at DESC
                LIMIT 30
                """
            ).fetchall()

            # In-app reactions per user
            app_reactions = conn.execute(
                """
                SELECT r.user, r.reaction, m.title, m.year, r.media_type, m.genres_json
                FROM reactions r
                JOIN media_items m
                  ON m.trakt_id = r.trakt_id AND m.media_type = r.media_type
                ORDER BY r.updated_at DESC
                """
            ).fetchall()

            # Netflix CSV history (especially Shelly's)
            netflix_rows = conn.execute(
                """
                SELECT user, base_title, media_type, play_count, last_watched
                FROM netflix_history
                ORDER BY play_count DESC, last_watched DESC
                LIMIT 80
                """
            ).fetchall()

            # Recent exclusion sample for prompt guidance (Python filters 100% strictly after lookup)
            recent_watched_titles = conn.execute(
                """
                SELECT m.title, m.year
                FROM trakt_history th
                JOIN media_items m
                  ON m.trakt_id = th.trakt_id AND m.media_type = th.media_type
                WHERE th.source IN ('watched', 'collection', 'watchlist')
                ORDER BY th.last_watched_at DESC
                LIMIT 120
                """
            ).fetchall()

        def fmt_genres(raw: str | None) -> str:
            if not raw:
                return ""
            try:
                g = json.loads(raw)
                return f" [{', '.join(g[:3])}]" if g else ""
            except Exception:
                return ""

        mark_likes = [
            f"{r['title']} ({r['year'] or '?'}) [{r['media_type']}, {r['user_rating']}/10]{fmt_genres(r['genres_json'])}"
            for r in mark_rated
        ]
        mark_shows = [f"{r['title']} ({r['year'] or '?'}) [{r['plays']} eps]" for r in mark_top_shows]
        mark_rec = [f"{r['title']} ({r['year'] or '?'}) [{r['media_type']}]" for r in mark_recent]
        mark_dislikes = [f"{r['title']} ({r['year'] or '?'}) [{r['user_rating']}/10]" for r in mark_disliked_trakt]

        shelly_likes: list[str] = []
        shelly_wants: list[str] = []
        shelly_dislikes: list[str] = []
        mark_wants: list[str] = []

        for r in app_reactions:
            label = f"{r['title']} ({r['year'] or '?'}) [{r['media_type']}]{fmt_genres(r['genres_json'])}"
            if r["user"] == "shelly":
                if r["reaction"] == "seen_loved":
                    shelly_likes.append(label)
                elif r["reaction"] == "want":
                    shelly_wants.append(label)
                elif r["reaction"] in ("pass", "seen_disliked"):
                    shelly_dislikes.append(label)
            else:
                if r["reaction"] == "seen_loved":
                    mark_likes.insert(0, label)
                elif r["reaction"] == "want":
                    mark_wants.append(label)
                elif r["reaction"] in ("pass", "seen_disliked"):
                    mark_dislikes.insert(0, label)

        shelly_netflix = [
            f"{r['base_title']} [{r['media_type']}, {r['play_count']} plays]"
            for r in netflix_rows
            if r["user"] == "shelly"
        ][:40]
        mark_netflix = [
            f"{r['base_title']} [{r['media_type']}, {r['play_count']} plays]"
            for r in netflix_rows
            if r["user"] == "mark"
        ][:20]

        avoid_list = [f"{r['title']} ({r['year'] or '?'})" for r in recent_watched_titles]
        avoid_list.extend(r["base_title"] for r in netflix_rows[:60])

        return {
            "mark_likes": mark_likes[:40],
            "mark_top_shows": mark_shows,
            "mark_recent": mark_rec,
            "mark_wants": mark_wants[:20],
            "mark_dislikes": mark_dislikes[:25],
            "mark_netflix": mark_netflix,
            "shelly_likes": shelly_likes[:35],
            "shelly_wants": shelly_wants[:20],
            "shelly_dislikes": shelly_dislikes[:25],
            "shelly_netflix": shelly_netflix,
            "avoid_sample": avoid_list[:150],
        }
