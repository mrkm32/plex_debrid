"""Netflix ViewingActivity CSV parser and background Trakt matcher."""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from collections import defaultdict
from typing import Any

from webapp.db import WatchNightDB
from webapp.trakt_client import TraktClient

# Common Netflix episode suffixes:
#   "Bridgerton: Season 2: Episode 1"
#   "The Queen's Gambit: Limited Series: Openings"
#   "Stranger Things: Stranger Things 4: Chapter One: The Hellfire Club"
#   "Money Heist: Part 3: We're Back"
_SHOW_MARKER_RE = re.compile(
    r":\s*(?:"
    r"(?:Season|Series)\s*\d+|"
    r"(?:Limited Series|Miniseries)|"
    r"(?:Episode|Ep\.?)\s*\d+|"
    r"S\d+(?:E\d+)?\b|"
    r"(?:Part|Volume|Vol\.?|Book|Chapter|Collection|Installment)\s+[^:]+:"
    r")",
    re.IGNORECASE,
)


def parse_netflix_csv(csv_text: str) -> list[dict[str, Any]]:
    """Parse Netflix ViewingActivity.csv / NetflixViewingHistory.csv into deduplicated titles."""
    cleaned = (csv_text or "").lstrip("\ufeff").strip()
    if not cleaned:
        return []

    reader = csv.reader(io.StringIO(cleaned))
    rows = [r for r in reader if r and any(cell.strip() for cell in r)]
    if not rows:
        return []

    header = [c.strip().lower() for c in rows[0]]
    title_idx = None
    date_idx = None

    for idx, col in enumerate(header):
        if col in ("title", "show name", "name"):
            title_idx = idx
        elif col in ("date", "start time", "watched at", "date watched"):
            date_idx = idx

    data_rows = rows[1:] if title_idx is not None else rows
    if title_idx is None:
        title_idx = 0
        date_idx = 1 if len(rows[0]) > 1 else None

    raw_entries: list[tuple[str, str]] = []
    prefix_counts: dict[str, int] = defaultdict(int)

    for row in data_rows:
        if len(row) <= title_idx:
            continue
        raw_title = row[title_idx].strip()
        if not raw_title:
            continue
        date_str = row[date_idx].strip() if (date_idx is not None and len(row) > date_idx) else ""
        raw_entries.append((raw_title, date_str))
        if ":" in raw_title:
            prefix = raw_title.split(":", 1)[0].strip()
            if prefix:
                prefix_counts[prefix.lower()] += 1

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_title, date_str in raw_entries:
        marker = _SHOW_MARKER_RE.search(raw_title)
        if marker:
            base_title = raw_title[: marker.start()].strip()
            media_type = "show"
        elif ":" in raw_title:
            first_part, rest = raw_title.split(":", 1)
            first_clean = first_part.strip()
            # If the prefix appears 3+ times (multiple episodes of a show without "Season X" label),
            # treat it as a TV show; otherwise keep as movie title (e.g., "Dune: Part Two").
            if first_clean and prefix_counts.get(first_clean.lower(), 0) >= 3 and rest.strip():
                base_title = first_clean
                media_type = "show"
            else:
                base_title = raw_title
                media_type = "movie"
        else:
            base_title = raw_title
            media_type = "movie"

        if not base_title:
            continue
        key = (base_title.lower(), media_type)
        if key not in grouped:
            grouped[key] = {
                "base_title": base_title,
                "media_type": media_type,
                "play_count": 0,
                "last_watched": date_str,
            }
        grouped[key]["play_count"] += 1
        if date_str and (not grouped[key]["last_watched"] or date_str > grouped[key]["last_watched"]):
            grouped[key]["last_watched"] = date_str

    result = list(grouped.values())
    result.sort(key=lambda x: (x["play_count"], x["last_watched"]), reverse=True)
    return result


def resolve_netflix_titles_in_background(
    db: WatchNightDB,
    trakt: TraktClient,
    user: str,
    limit: int = 40,
) -> threading.Thread:
    """Resolve top unresolved Netflix titles to Trakt IDs without blocking the upload response."""

    def _worker() -> None:
        rows = db.get_unresolved_netflix_titles(user, limit=limit)
        for row in rows:
            try:
                resolved = trakt.resolve_title(
                    row["base_title"],
                    media_type=row["media_type"],
                    db=db,
                )
                if resolved and resolved.get("trakt_id"):
                    db.update_netflix_trakt_id(row["id"], int(resolved["trakt_id"]))
            except Exception:
                pass
            time.sleep(0.25)

    t = threading.Thread(target=_worker, daemon=True, name=f"netflix-resolve-{user}")
    t.start()
    return t
