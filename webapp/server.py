"""Starlette ASGI backend and PWA static server for WatchNight."""

from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import qrcode
import requests
import uvicorn
from PIL import Image, ImageDraw
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from webapp.ai_recommender import (
    generate_recommendations,
    generate_trakt_fallback,
    test_ai_connection,
)
from webapp.alldebrid_client import AllDebridClient
from webapp.config import (
    IMAGE_CACHE_DIR,
    PORT,
    PROJECT_DIR,
    RECOMMENDER_CONFIG_PATH,
    SETTINGS_PATH,
    STATIC_DIR,
    ensure_data_dirs,
    load_recommender_config,
    public_config,
    save_recommender_config,
)
from webapp.db import VALID_MEDIA_TYPES, VALID_REACTIONS, VALID_USERS, WatchNightDB
from webapp.netflix_import import parse_netflix_csv, resolve_netflix_titles_in_background
from webapp.trakt_client import TraktClient

ALLOWED_IMAGE_HOSTS = {"media.trakt.tv", "walter-r2.trakt.tv", "image.tmdb.org"}
TRAKT_SYNC_INTERVAL_SECS = 6 * 3600

# Curated cross-platform hits so Shelly (Netflix/Prime/Hulu/Apple TV+) can seed favorites in 60 seconds.
SEEDER_TITLES: list[dict[str, Any]] = [
    {"title": "Nobody Wants This", "year": 2024, "media_type": "show"},
    {"title": "The White Lotus", "year": 2021, "media_type": "show"},
    {"title": "Only Murders in the Building", "year": 2021, "media_type": "show"},
    {"title": "Ted Lasso", "year": 2020, "media_type": "show"},
    {"title": "Schitt's Creek", "year": 2015, "media_type": "show"},
    {"title": "The Bear", "year": 2022, "media_type": "show"},
    {"title": "Severance", "year": 2022, "media_type": "show"},
    {"title": "Bridgerton", "year": 2020, "media_type": "show"},
    {"title": "The Queen's Gambit", "year": 2020, "media_type": "show"},
    {"title": "Big Little Lies", "year": 2017, "media_type": "show"},
    {"title": "Dead to Me", "year": 2019, "media_type": "show"},
    {"title": "Shrinking", "year": 2023, "media_type": "show"},
    {"title": "Bad Sisters", "year": 2022, "media_type": "show"},
    {"title": "Hacks", "year": 2021, "media_type": "show"},
    {"title": "The Marvelous Mrs. Maisel", "year": 2017, "media_type": "show"},
    {"title": "Fleabag", "year": 2016, "media_type": "show"},
    {"title": "Beef", "year": 2023, "media_type": "show"},
    {"title": "Abbott Elementary", "year": 2021, "media_type": "show"},
    {"title": "Slow Horses", "year": 2022, "media_type": "show"},
    {"title": "Stranger Things", "year": 2016, "media_type": "show"},
    {"title": "Fallout", "year": 2024, "media_type": "show"},
    {"title": "Reacher", "year": 2022, "media_type": "show"},
    {"title": "Wednesday", "year": 2022, "media_type": "show"},
    {"title": "The Diplomat", "year": 2023, "media_type": "show"},
    {"title": "Knives Out", "year": 2019, "media_type": "movie"},
    {"title": "Glass Onion", "year": 2022, "media_type": "movie"},
    {"title": "Palm Springs", "year": 2020, "media_type": "movie"},
    {"title": "Crazy Rich Asians", "year": 2018, "media_type": "movie"},
    {"title": "Anyone But You", "year": 2023, "media_type": "movie"},
    {"title": "The Holdovers", "year": 2023, "media_type": "movie"},
    {"title": "Past Lives", "year": 2023, "media_type": "movie"},
    {"title": "Everything Everywhere All at Once", "year": 2022, "media_type": "movie"},
    {"title": "Parasite", "year": 2019, "media_type": "movie"},
    {"title": "The Fall Guy", "year": 2024, "media_type": "movie"},
    {"title": "Top Gun: Maverick", "year": 2022, "media_type": "movie"},
    {"title": "Game Night", "year": 2018, "media_type": "movie"},
]


def ensure_pwa_icons(static_dir: Path = STATIC_DIR) -> None:
    """Generate clean WatchNight app icons on disk if missing."""
    static_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),
    ]
    for filename, size in specs:
        out = static_dir / filename
        if out.exists():
            continue
        img = Image.new("RGBA", (size, size), (10, 13, 20, 255))
        draw = ImageDraw.Draw(img)
        pad = int(size * 0.08)
        draw.rounded_rectangle(
            [pad, pad, size - pad, size - pad],
            radius=int(size * 0.22),
            fill=(21, 27, 40, 255),
            outline=(255, 90, 95, 220),
            width=max(2, size // 48),
        )
        # Dual overlapping cinema rings (two profiles -> one match)
        r = int(size * 0.20)
        cy = int(size * 0.48)
        cx1 = int(size * 0.41)
        cx2 = int(size * 0.59)
        draw.ellipse([cx1 - r, cy - r, cx1 + r, cy + r], outline=(255, 90, 95, 255), width=max(4, size // 26))
        draw.ellipse([cx2 - r, cy - r, cx2 + r, cy + r], outline=(245, 185, 66, 255), width=max(4, size // 26))
        # Center play triangle
        tri = [
            (int(size * 0.46), int(size * 0.41)),
            (int(size * 0.46), int(size * 0.55)),
            (int(size * 0.58), int(size * 0.48)),
        ]
        draw.polygon(tri, fill=(248, 250, 252, 255))
        img.convert("RGB").save(out, format="PNG")


def _send_match_imessage_async(title: str, year: int | None) -> None:
    def _worker() -> None:
        try:
            scripts_dir = str(PROJECT_DIR / "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            from alerts import send_alert

            yr = f" ({year})" if year else ""
            send_alert(
                title="WatchNight Match",
                message=f"You & Shelly both picked {title}{yr} — sending to Infuse now!",
                error_key=f"watchnight_match_{title.lower()}",
                force=True,
            )
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def create_app(
    db: WatchNightDB | None = None,
    trakt: TraktClient | None = None,
    alldebrid: AllDebridClient | None = None,
    config_path: Path | None = None,
    settings_path: Path | None = None,
    image_cache_dir: Path | None = None,
    start_background_sync: bool = True,
) -> Starlette:
    ensure_data_dirs()
    ensure_pwa_icons(STATIC_DIR)

    store = db or WatchNightDB()
    cfg_path = config_path or RECOMMENDER_CONFIG_PATH
    set_path = settings_path or SETTINGS_PATH
    trakt_cli = trakt or TraktClient(settings_path=set_path)
    ad_cli = alldebrid or AllDebridClient(settings_path=set_path)
    img_dir = image_cache_dir or IMAGE_CACHE_DIR
    img_dir.mkdir(parents=True, exist_ok=True)
    seeder_lock = threading.Lock()
    stop_event = threading.Event()

    def _enrich_items(items: list[dict[str, Any]], user: str) -> list[dict[str, Any]]:
        reactions = store.get_user_reactions(user)
        partner_wants = store.get_partner_want_set(user)
        partner_name = "Shelly" if store.get_partner(user) == "shelly" else "Mark"
        collection_set = store.get_trakt_source_set("collection")
        status_map = store.get_latest_infuse_status_map()

        enriched: list[dict[str, Any]] = []
        for raw in items:
            item = dict(raw)
            if "poster_proxy" not in item:
                item = store.row_to_media_dict(item)
            tid = item.get("trakt_id")
            mt = item.get("media_type")
            if tid and mt:
                key = (int(tid), mt)
                item["user_reaction"] = reactions.get(key)
                item["partner_wanted"] = key in partner_wants
                item["partner_name"] = partner_name
                req = status_map.get(key)
                if key in collection_set:
                    item["in_infuse"] = True
                    item["infuse_status"] = "ready"
                elif req:
                    item["infuse_status"] = req["status"]
                    item["in_infuse"] = req["status"] == "ready"
                else:
                    item["infuse_status"] = None
                    item["in_infuse"] = False
            enriched.append(item)
        return enriched

    def _send_to_infuse_sync(trakt_id: int, media_type: str, requested_by: str) -> dict[str, Any]:
        item = store.get_media_item(trakt_id, media_type)
        title = (item or {}).get("title") or f"Title #{trakt_id}"
        year = (item or {}).get("year")

        try:
            collection_ids, _ = trakt_cli.get_collection_and_watchlist_ids(db=store)
        except Exception:
            collection_ids = store.get_trakt_source_set("collection")

        if (int(trakt_id), media_type) in collection_ids:
            req = store.create_infuse_request(
                trakt_id=trakt_id,
                media_type=media_type,
                title=title,
                year=year,
                requested_by=requested_by,
                status="ready",
                status_detail="Already in your Infuse / Trakt Collection",
            )
            return req

        trakt_cli.add_to_watchlist(trakt_id, media_type)
        req = store.create_infuse_request(
            trakt_id=trakt_id,
            media_type=media_type,
            title=title,
            year=year,
            requested_by=requested_by,
            status="queued",
            status_detail="Added to Trakt watchlist — plex_debrid is grabbing it...",
        )
        return req

    def _refresh_infuse_requests_sync() -> dict[str, Any]:
        pending = store.get_pending_infuse_requests()
        if pending:
            try:
                collection_ids, _ = trakt_cli.get_collection_and_watchlist_ids(db=store, force_refresh=True)
            except Exception:
                collection_ids = store.get_trakt_source_set("collection")

            now = time.time()
            for req in pending:
                key = (int(req["trakt_id"]), req["media_type"])
                if key in collection_ids:
                    store.update_infuse_request_status(
                        req["id"],
                        "ready",
                        "Added to Trakt Collection & ready in Infuse",
                    )
                    continue
                if ad_cli.is_title_ready(req["title"], req["media_type"], req.get("year")):
                    store.update_infuse_request_status(
                        req["id"],
                        "ready",
                        "Cached on AllDebrid — ready in Infuse",
                    )
                    continue
                age = now - float(req.get("requested_at") or now)
                if age > 45 and req["status"] == "queued":
                    store.update_infuse_request_status(
                        req["id"],
                        "searching",
                        "Waiting on AllDebrid / release scraper...",
                    )

        return {
            "requests": store.get_infuse_requests(50),
            "matches": store.get_mutual_matches(),
        }

    def _get_seeder_items_sync(user: str) -> list[dict[str, Any]]:
        with seeder_lock:
            cached_raw, _ = store.get_sync_meta("seeder_catalog_v1")
            resolved_keys: list[dict[str, Any]] = []
            if cached_raw:
                try:
                    resolved_keys = json.loads(cached_raw)
                except Exception:
                    resolved_keys = []

            if not resolved_keys:
                for entry in SEEDER_TITLES:
                    try:
                        found = trakt_cli.resolve_title(
                            entry["title"],
                            year=entry.get("year"),
                            media_type=entry.get("media_type"),
                            db=store,
                        )
                        if found and found.get("trakt_id"):
                            resolved_keys.append(
                                {
                                    "trakt_id": int(found["trakt_id"]),
                                    "media_type": found["media_type"],
                                }
                            )
                    except Exception:
                        pass
                    time.sleep(0.08)
                if resolved_keys:
                    store.set_sync_meta("seeder_catalog_v1", resolved_keys)

        items: list[dict[str, Any]] = []
        for rk in resolved_keys:
            m = store.get_media_item(rk["trakt_id"], rk["media_type"])
            if m:
                items.append(m)
        return _enrich_items(items, user)

    def _periodic_sync_loop() -> None:
        # Initial check on startup: sync if never synced or older than 6 hours
        while not stop_event.is_set():
            try:
                _, last_at = store.get_sync_meta("last_trakt_sync")
                age = time.time() - float(last_at or 0)
                if not last_at or age >= TRAKT_SYNC_INTERVAL_SECS:
                    trakt_cli.sync_trakt_to_db(store)
            except Exception:
                pass
            if stop_event.wait(1800):
                break

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        if start_background_sync:
            threading.Thread(target=_periodic_sync_loop, daemon=True, name="trakt-bg-sync").start()
        try:
            yield
        finally:
            stop_event.set()

    # ---------- Routes ----------

    async def home(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def manifest(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")

    async def api_health(request: Request) -> JSONResponse:
        cfg = load_recommender_config(cfg_path)
        _, synced_at = store.get_sync_meta("last_trakt_sync")
        return JSONResponse(
            {
                "ok": True,
                "version": "1.0",
                "trakt_synced_at": synced_at,
                "has_ai_key": bool(str(cfg.get("ai_api_key") or "").strip()),
            }
        )

    async def api_bootstrap(request: Request) -> JSONResponse:
        user = request.query_params.get("user", "shelly").lower()
        if user not in VALID_USERS:
            user = "shelly"
        cfg = load_recommender_config(cfg_path)
        stats = store.get_trakt_sync_stats()
        react_counts = store.get_user_reaction_counts(user)
        matches = store.get_mutual_matches()
        pending = store.get_pending_infuse_requests()
        show_shelly_nudge = user == "shelly" and (react_counts["positives"] + react_counts["netflix_items"]) < 5
        return JSONResponse(
            {
                "config": public_config(cfg, cfg_path),
                "sync_stats": stats,
                "reaction_counts": react_counts,
                "match_count": len(matches),
                "active_infuse_count": len(pending),
                "show_shelly_nudge": show_shelly_nudge,
            }
        )

    async def api_deck(request: Request) -> JSONResponse:
        mode = request.query_params.get("mode", "together").lower()
        if mode not in ("together", "shelly", "mark"):
            mode = "together"
        user = request.query_params.get("user", "shelly").lower()
        if user not in VALID_USERS:
            user = "shelly"
        media_type = request.query_params.get("media_type", "both").lower()
        if media_type not in ("both", "movie", "show"):
            media_type = "both"

        cards = store.get_deck(mode=mode, user=user, media_type=media_type, limit=25)
        source = cards[0].get("rec_source") if cards else None
        warning = None

        if not cards:
            # Check if this mode has never had recommendations generated yet
            with store._connect() as conn:
                has_any = conn.execute("SELECT 1 FROM recommendations WHERE mode = ? LIMIT 1", (mode,)).fetchone()
            cfg = load_recommender_config(cfg_path)
            if not has_any or not str(cfg.get("ai_api_key") or "").strip():
                res = await run_in_threadpool(
                    generate_trakt_fallback,
                    store,
                    trakt_cli,
                    mode,
                    user,
                    media_type,
                    "",
                    12,
                )
                cards = res.get("cards") or []
                source = res.get("source")
                if not str(cfg.get("ai_api_key") or "").strip():
                    warning = "Showing smart Trakt picks. Add an AI key in Settings for personalized AI picks!"

        return JSONResponse({"cards": cards, "source": source, "warning": warning})

    async def api_generate(request: Request) -> JSONResponse:
        body = await request.json()
        mode = str(body.get("mode") or "together").lower()
        if mode not in ("together", "shelly", "mark"):
            mode = "together"
        user = str(body.get("user") or ("shelly" if mode == "shelly" else "mark" if mode == "mark" else "shelly")).lower()
        if user not in VALID_USERS:
            user = "shelly"
        mood = str(body.get("mood") or "").strip()
        media_type = str(body.get("media_type") or "both").lower()
        if media_type not in ("both", "movie", "show"):
            media_type = "both"

        cfg = load_recommender_config(cfg_path)
        res = await run_in_threadpool(
            generate_recommendations,
            store,
            trakt_cli,
            cfg,
            mode,
            user,
            mood,
            media_type,
            12,
        )
        return JSONResponse(res)

    async def api_react(request: Request) -> JSONResponse:
        if request.method == "DELETE":
            user = request.query_params.get("user", "").lower()
            tid = request.query_params.get("trakt_id")
            mt = request.query_params.get("media_type", "")
            if user not in VALID_USERS or not tid or mt not in VALID_MEDIA_TYPES:
                return JSONResponse({"error": "Invalid parameters"}, status_code=400)
            store.delete_reaction(user, int(tid), mt)
            return JSONResponse({"ok": True})

        body = await request.json()
        user = str(body.get("user") or "").lower()
        reaction = str(body.get("reaction") or "").lower()
        mt = str(body.get("media_type") or "").lower()
        trakt_id = body.get("trakt_id")

        if user not in VALID_USERS or reaction not in VALID_REACTIONS or mt not in VALID_MEDIA_TYPES or not trakt_id:
            return JSONResponse({"error": "Invalid user, media_type, trakt_id, or reaction"}, status_code=400)

        if isinstance(body.get("item"), dict):
            store.upsert_media_item(body["item"])

        store.set_reaction(user, int(trakt_id), mt, reaction)

        matched = False
        auto_sent = False
        infuse_req = None

        if reaction == "want":
            matched = store.check_mutual_match(int(trakt_id), mt)
            if matched:
                cfg = load_recommender_config(cfg_path)
                if cfg.get("auto_send_matches", True):
                    try:
                        infuse_req = await run_in_threadpool(_send_to_infuse_sync, int(trakt_id), mt, "match")
                        auto_sent = True
                    except Exception:
                        auto_sent = False
                if cfg.get("imessage_on_match", False):
                    item = store.get_media_item(int(trakt_id), mt) or {}
                    _send_match_imessage_async(item.get("title") or f"Title #{trakt_id}", item.get("year"))

        return JSONResponse(
            {
                "ok": True,
                "matched": matched,
                "auto_sent": auto_sent,
                "infuse_request": infuse_req,
            }
        )

    async def api_search(request: Request) -> JSONResponse:
        q = request.query_params.get("q", "").strip()
        media_type = request.query_params.get("type", "movie,show").strip()
        user = request.query_params.get("user", "shelly").lower()
        if user not in VALID_USERS:
            user = "shelly"
        if media_type not in ("movie", "show", "movie,show"):
            media_type = "movie,show"
        if not q:
            return JSONResponse({"results": []})
        items = await run_in_threadpool(trakt_cli.search_trakt, q, media_type, 15, store)
        return JSONResponse({"results": _enrich_items(items, user)})

    async def api_browse(request: Request) -> JSONResponse:
        category = request.query_params.get("category", "trending_shows").strip()
        user = request.query_params.get("user", "shelly").lower()
        if user not in VALID_USERS:
            user = "shelly"
        if category not in ("trending_shows", "trending_movies", "popular_shows", "popular_movies", "romcoms"):
            return JSONResponse({"error": "Invalid category"}, status_code=400)
        items = await run_in_threadpool(trakt_cli.get_browse_list, category, 20, store)
        return JSONResponse({"category": category, "results": _enrich_items(items, user)})

    async def api_seeder(request: Request) -> JSONResponse:
        user = request.query_params.get("user", "shelly").lower()
        if user not in VALID_USERS:
            user = "shelly"
        items = await run_in_threadpool(_get_seeder_items_sync, user)
        return JSONResponse({"items": items})

    async def api_infuse_send(request: Request) -> JSONResponse:
        body = await request.json()
        trakt_id = body.get("trakt_id")
        mt = str(body.get("media_type") or "").lower()
        user = str(body.get("user") or "shelly").lower()
        if not trakt_id or mt not in VALID_MEDIA_TYPES:
            return JSONResponse({"error": "Missing trakt_id or valid media_type"}, status_code=400)
        if user not in VALID_USERS:
            user = "shelly"
        if isinstance(body.get("item"), dict):
            store.upsert_media_item(body["item"])
        try:
            req = await run_in_threadpool(_send_to_infuse_sync, int(trakt_id), mt, user)
            return JSONResponse({"ok": True, "request": req})
        except PermissionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    async def api_infuse_status(request: Request) -> JSONResponse:
        data = await run_in_threadpool(_refresh_infuse_requests_sync)
        return JSONResponse(data)

    async def api_import_netflix(request: Request) -> JSONResponse:
        form = await request.form()
        user = str(form.get("user") or "shelly").lower()
        if user not in VALID_USERS:
            user = "shelly"
        upload = form.get("file")
        if upload is None:
            return JSONResponse({"ok": False, "error": "Please upload a CSV file."}, status_code=400)
        raw_bytes = await upload.read()
        csv_text = raw_bytes.decode("utf-8", errors="replace")
        entries = parse_netflix_csv(csv_text)
        if not entries:
            return JSONResponse(
                {"ok": False, "error": "Could not find any watched titles in that CSV."},
                status_code=400,
            )
        shows, movies = store.upsert_netflix_history(user, entries)
        resolve_netflix_titles_in_background(store, trakt_cli, user, limit=40)
        return JSONResponse(
            {
                "ok": True,
                "user": user,
                "shows_imported": shows,
                "movies_imported": movies,
                "total_unique": len(entries),
                "top_titles": entries[:15],
            }
        )

    async def api_taste(request: Request) -> JSONResponse:
        user = request.query_params.get("user", "shelly").lower()
        if user not in VALID_USERS:
            user = "shelly"
        details = store.get_user_taste_details(user)
        cfg = load_recommender_config(cfg_path)
        details["taste_notes"] = cfg.get("taste_notes") or {"mark": "", "shelly": "", "together": ""}
        return JSONResponse(details)

    async def api_trakt_sync(request: Request) -> JSONResponse:
        try:
            res = await run_in_threadpool(trakt_cli.sync_trakt_to_db, store)
            return JSONResponse({"ok": True, **res})
        except PermissionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    async def api_settings(request: Request) -> JSONResponse:
        if request.method == "GET":
            return JSONResponse(public_config(config_path=cfg_path))
        body = await request.json()
        updated = save_recommender_config(body, config_path=cfg_path)
        return JSONResponse(public_config(updated, cfg_path))

    async def api_test_ai(request: Request) -> JSONResponse:
        body = await request.json()
        cfg = load_recommender_config(cfg_path)
        provider = str(body.get("ai_provider") or cfg.get("ai_provider") or "gemini")
        model = str(body.get("ai_model") or cfg.get("ai_model") or "")
        api_key = str(body.get("ai_api_key") or "").strip() or str(cfg.get("ai_api_key") or "").strip()
        result = await run_in_threadpool(test_ai_connection, provider, model, api_key)
        return JSONResponse(result)

    async def api_image(request: Request) -> Response:
        raw_url = request.query_params.get("url", "").strip()
        if not raw_url:
            return Response(status_code=400)
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or parsed.netloc.lower() not in ALLOWED_IMAGE_HOSTS:
            return Response(content="Forbidden image host", status_code=403)

        ext = Path(parsed.path).suffix.lower()
        if ext not in (".webp", ".jpg", ".jpeg", ".png"):
            ext = ".webp"
        digest = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()
        cache_file = img_dir / f"{digest}{ext}"
        media_types = {
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
        }

        if cache_file.exists() and cache_file.stat().st_size > 0:
            return FileResponse(
                cache_file,
                media_type=media_types.get(ext, "image/webp"),
                headers={"Cache-Control": "public, max-age=604800"},
            )

        def _download_image() -> bytes | None:
            resp = requests.get(
                raw_url,
                headers={"User-Agent": "WatchNight/1.0 (plex_debrid)"},
                timeout=12,
            )
            if resp.status_code != 200 or not resp.content or len(resp.content) > 5 * 1024 * 1024:
                return None
            tmp_file = img_dir / f"{digest}{ext}.tmp"
            tmp_file.write_bytes(resp.content)
            tmp_file.replace(cache_file)
            return resp.content

        data = await run_in_threadpool(_download_image)
        if not data:
            return Response(status_code=404)
        return Response(
            content=data,
            media_type=media_types.get(ext, "image/webp"),
            headers={"Cache-Control": "public, max-age=604800"},
        )

    async def api_qr(request: Request) -> Response:
        pub = public_config(config_path=cfg_path)
        target_url = pub["lan"]["local_url"]
        mode = request.query_params.get("kind", "local")
        if mode == "ip":
            target_url = pub["lan"]["ip_url"]
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(target_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0A0D14", back_color="#FFFFFF").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "no-cache"},
        )

    routes = [
        Route("/", home, methods=["GET"]),
        Route("/manifest.json", manifest, methods=["GET"]),
        Route("/api/health", api_health, methods=["GET"]),
        Route("/api/bootstrap", api_bootstrap, methods=["GET"]),
        Route("/api/deck", api_deck, methods=["GET"]),
        Route("/api/recommendations/generate", api_generate, methods=["POST"]),
        Route("/api/react", api_react, methods=["POST", "DELETE"]),
        Route("/api/search", api_search, methods=["GET"]),
        Route("/api/browse", api_browse, methods=["GET"]),
        Route("/api/seeder", api_seeder, methods=["GET"]),
        Route("/api/infuse/send", api_infuse_send, methods=["POST"]),
        Route("/api/infuse/status", api_infuse_status, methods=["GET"]),
        Route("/api/import/netflix", api_import_netflix, methods=["POST"]),
        Route("/api/taste", api_taste, methods=["GET"]),
        Route("/api/trakt/sync", api_trakt_sync, methods=["POST"]),
        Route("/api/settings", api_settings, methods=["GET", "POST"]),
        Route("/api/settings/test-ai", api_test_ai, methods=["POST"]),
        Route("/api/image", api_image, methods=["GET"]),
        Route("/api/qr", api_qr, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    app = Starlette(
        debug=False,
        routes=routes,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.db = store
    app.state.trakt = trakt_cli
    app.state.alldebrid = ad_cli
    return app


app = create_app(start_background_sync=(__name__ == "__main__"))

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("WATCHNIGHT_PORT", PORT)),
        log_level="info",
    )
