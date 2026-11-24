"""Automated tests for WatchNight (all external APIs mocked)."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
import requests
import uvicorn

from webapp.ai_recommender import (
    build_taste_prompt,
    generate_recommendations,
    parse_ai_json,
    test_ai_connection as check_ai_connection,
)
from webapp.alldebrid_client import AllDebridClient
from webapp.config import load_recommender_config, save_recommender_config
from webapp.db import WatchNightDB
from webapp.netflix_import import parse_netflix_csv
from webapp.server import create_app
from webapp.trakt_client import TraktClient


def _sample_item(trakt_id: int, media_type: str, title: str, year: int = 2023) -> dict:
    return {
        "trakt_id": trakt_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "imdb_id": f"tt{trakt_id}",
        "tmdb_id": trakt_id + 1000,
        "slug": title.lower().replace(" ", "-"),
        "overview": f"Synopsis for {title}",
        "tagline": f"Tagline for {title}",
        "rating": 8.4,
        "votes": 5000,
        "runtime": 48 if media_type == "show" else 115,
        "genres": ["drama", "thriller"],
        "poster_url": f"https://media.trakt.tv/{media_type}s/{trakt_id}/medium/poster.webp",
        "fanart_url": f"https://media.trakt.tv/{media_type}s/{trakt_id}/medium/fanart.webp",
        "trailer_url": "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "certification": "TV-MA" if media_type == "show" else "R",
    }


@contextmanager
def _running_test_server(app) -> Iterator[tuple[str, requests.Session]]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(128)
    sock.setblocking(False)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    session = requests.Session()
    try:
        yield f"http://127.0.0.1:{port}", session
    finally:
        session.close()
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()


def test_db_upsert_preserves_extended_metadata_and_exclusions(tmp_path: Path) -> None:
    db = WatchNightDB(tmp_path / "test.db")
    rich = _sample_item(101, "show", "Severance", 2022)
    db.upsert_media_item(rich)

    # Simulate lightweight watched sync missing overview & poster
    db.upsert_media_item({"trakt_id": 101, "media_type": "show", "title": "Severance", "year": 2022})
    stored = db.get_media_item(101, "show")
    assert stored is not None
    assert stored["overview"] == "Synopsis for Severance"
    assert stored["poster_url"] == rich["poster_url"]
    assert stored["genres"] == ["drama", "thriller"]

    # Add watched history + Shelly want + Mark pass on another title
    db.replace_trakt_history_source("watched", [{"trakt_id": 101, "media_type": "show", "plays": 9}])
    db.upsert_media_item(_sample_item(202, "movie", "Palm Springs", 2020))
    db.upsert_media_item(_sample_item(303, "movie", "Oldboy", 2003))
    db.set_reaction("shelly", 202, "movie", "want")
    db.set_reaction("shelly", 303, "movie", "pass")

    # For Mark in together mode: 101 (watched) and 303 (Shelly passed) are excluded,
    # while 202 (Shelly wants!) remains eligible so Mark can match on it.
    excl_keys, excl_titles = db.get_exclusion_sets(mode="together", user="mark")
    assert (101, "show") in excl_keys
    assert (303, "movie") in excl_keys
    assert (202, "movie") not in excl_keys
    assert "severance" in excl_titles


def test_netflix_csv_parser_grouping_and_bom() -> None:
    csv_text = (
        "\ufeffTitle,Date\n"
        '"Bridgerton: Season 2: Episode 1","04/12/2024"\n'
        '"Bridgerton: Season 2: Episode 2","04/13/2024"\n'
        '"Dune: Part Two","05/01/2024"\n'
        '"The Queen\'s Gambit: Limited Series: Openings","01/10/2024"\n'
        '"The Office: Diversity Day","02/01/2024"\n'
        '"The Office: Health Care","02/02/2024"\n'
        '"The Office: The Alliance","02/03/2024"\n'
        '"Palm Springs","03/15/2024"\n'
    )
    entries = parse_netflix_csv(csv_text)
    by_title = {(e["base_title"], e["media_type"]): e for e in entries}

    assert by_title[("Bridgerton", "show")]["play_count"] == 2
    assert by_title[("Bridgerton", "show")]["last_watched"] == "04/13/2024"
    assert by_title[("Dune: Part Two", "movie")]["play_count"] == 1
    assert by_title[("The Queen's Gambit", "show")]["play_count"] == 1
    assert by_title[("The Office", "show")]["play_count"] == 3
    assert by_title[("Palm Springs", "movie")]["play_count"] == 1


def test_parse_ai_json_variants() -> None:
    fenced = """Here are your picks:
    ```json
    {
      "recommendations": [
        {"title": "Bad Sisters", "year": 2022, "media_type": "show", "pitch": "Dark comedy mystery.", "tags": ["Witty", "Mystery"]},
        {"title": "Game Night", "year": 2018, "media_type": "movie", "pitch": "Fast comedy thriller.", "tags": ["Comedy"]}
      ]
    }
    ```"""
    parsed = parse_ai_json(fenced)
    assert len(parsed) == 2
    assert parsed[0]["title"] == "Bad Sisters"
    assert parsed[0]["media_type"] == "show"
    assert parsed[1]["media_type"] == "movie"


def test_ai_recommender_filters_watched_and_supports_all_providers(tmp_path: Path) -> None:
    db = WatchNightDB(tmp_path / "ai.db")
    db.upsert_media_item(_sample_item(10, "show", "Severance", 2022))
    db.replace_trakt_history_source("watched", [{"trakt_id": 10, "media_type": "show", "plays": 9}])
    db.upsert_netflix_history("shelly", [{"base_title": "Bridgerton", "media_type": "show", "play_count": 16}])

    trakt = MagicMock(spec=TraktClient)
    catalog = {
        "severance": _sample_item(10, "show", "Severance", 2022),
        "bridgerton": _sample_item(20, "show", "Bridgerton", 2020),
        "bad sisters": _sample_item(30, "show", "Bad Sisters", 2022),
        "the holdovers": _sample_item(40, "movie", "The Holdovers", 2023),
    }

    def fake_resolve(title: str, year=None, media_type=None, db=None):
        item = catalog.get(title.lower())
        if item and db:
            db.upsert_media_item(item)
        return item

    trakt.resolve_title.side_effect = fake_resolve

    ai_json_payload = json.dumps(
        {
            "recommendations": [
                {"title": "Severance", "year": 2022, "media_type": "show", "pitch": "Already watched on Trakt"},
                {"title": "Bridgerton", "year": 2020, "media_type": "show", "pitch": "Already watched on Netflix"},
                {"title": "Bad Sisters", "year": 2022, "media_type": "show", "pitch": "Fresh pick for both of you."},
                {"title": "The Holdovers", "year": 2023, "media_type": "movie", "pitch": "Warm funny drama."},
            ]
        }
    )

    for provider, mock_resp in (
        ("gemini", {"candidates": [{"content": {"parts": [{"text": ai_json_payload}]}}]}),
        ("openai", {"choices": [{"message": {"content": ai_json_payload}}]}),
        ("anthropic", {"content": [{"type": "text", "text": ai_json_payload}]}),
    ):
        resp_obj = MagicMock()
        resp_obj.status_code = 200
        resp_obj.json.return_value = mock_resp

        with patch("webapp.ai_recommender.requests.post", return_value=resp_obj):
            cfg = {
                "ai_provider": provider,
                "ai_model": "test-model",
                "ai_api_key": "secret-key",
                "taste_notes": {"mark": "Sci-fi", "shelly": "Cozy mystery", "together": "No gore"},
            }
            prompt = build_taste_prompt(db, cfg, mode="together")
            assert "No gore" in prompt
            res = generate_recommendations(db, trakt, cfg, mode="together", user="shelly")
            titles = [c["title"] for c in res["cards"]]
            assert "Severance" not in titles
            assert "Bridgerton" not in titles
            assert titles == ["Bad Sisters", "The Holdovers"]
            assert res["source"] == f"ai:{provider}"


def test_server_endpoints_match_infuse_masking_and_image_ssrf(tmp_path: Path) -> None:
    db = WatchNightDB(tmp_path / "srv.db")
    cfg_path = tmp_path / "rec_cfg.json"
    img_dir = tmp_path / "images"

    save_recommender_config(
        {"ai_provider": "gemini", "ai_api_key": "AIzaSecretTestKey1234", "auto_send_matches": True},
        config_path=cfg_path,
    )

    item_a = _sample_item(501, "movie", "Knives Out", 2019)
    item_b = _sample_item(502, "show", "Only Murders in the Building", 2021)
    db.upsert_media_items([item_a, item_b])

    mock_trakt = MagicMock(spec=TraktClient)
    mock_trakt.get_collection_and_watchlist_ids.return_value = (set(), set())
    mock_trakt.add_to_watchlist.return_value = {"added": {"movies": 1}}
    mock_trakt.get_related.return_value = []
    mock_trakt.get_browse_list.return_value = [item_a, item_b]

    mock_ad = MagicMock(spec=AllDebridClient)
    mock_ad.is_title_ready.return_value = False

    app = create_app(
        db=db,
        trakt=mock_trakt,
        alldebrid=mock_ad,
        config_path=cfg_path,
        image_cache_dir=img_dir,
        start_background_sync=False,
    )

    with _running_test_server(app) as (base_url, sess):
        # 1. Health, Bootstrap & Settings secret masking
        assert sess.get(f"{base_url}/api/health", timeout=5).json()["ok"] is True
        boot = sess.get(f"{base_url}/api/bootstrap", params={"user": "shelly"}, timeout=5).json()
        assert boot["show_shelly_nudge"] is True

        settings_res = sess.get(f"{base_url}/api/settings", timeout=5).json()
        assert settings_res["has_api_key"] is True
        assert "AIzaSecretTestKey1234" not in json.dumps(settings_res)
        assert settings_res["api_key_hint"] == "...1234"

        # Updating settings with blank key preserves existing secret
        sess.post(
            f"{base_url}/api/settings",
            json={"ai_provider": "openai", "ai_model": "gpt-6.2-sol", "ai_api_key": ""},
            timeout=5,
        )
        assert load_recommender_config(cfg_path)["ai_api_key"] == "AIzaSecretTestKey1234"

        # 2. Single-user want does NOT trigger match or Infuse send
        r1 = sess.post(
            f"{base_url}/api/react",
            json={"user": "shelly", "trakt_id": 501, "media_type": "movie", "reaction": "want"},
            timeout=5,
        ).json()
        assert r1["matched"] is False
        assert r1["auto_sent"] is False
        mock_trakt.add_to_watchlist.assert_not_called()

        # Partner want DOES trigger mutual match & auto-send to Infuse
        r2 = sess.post(
            f"{base_url}/api/react",
            json={"user": "mark", "trakt_id": 501, "media_type": "movie", "reaction": "want"},
            timeout=5,
        ).json()
        assert r2["matched"] is True
        assert r2["auto_sent"] is True
        mock_trakt.add_to_watchlist.assert_called_once_with(501, "movie")

        # 3. Infuse status flips queued -> ready when AllDebrid reports ready
        st1 = sess.get(f"{base_url}/api/infuse/status", timeout=5).json()
        assert st1["requests"][0]["status"] == "queued"
        mock_ad.is_title_ready.return_value = True
        st2 = sess.get(f"{base_url}/api/infuse/status", timeout=5).json()
        assert st2["requests"][0]["status"] == "ready"

        # 4. Netflix CSV import endpoint
        csv_bytes = (
            b"Title,Date\nBridgerton: Season 1: Episode 1,01/01/2024\n"
            b"Bridgerton: Season 1: Episode 2,01/02/2024\n"
        )
        imp = sess.post(
            f"{base_url}/api/import/netflix",
            data={"user": "shelly"},
            files={"file": ("NetflixViewingHistory.csv", io.BytesIO(csv_bytes), "text/csv")},
            timeout=5,
        )
        assert imp.status_code == 200
        assert imp.json()["shows_imported"] == 1

        # 5. Image proxy SSRF protection + caching
        assert sess.get(f"{base_url}/api/image", params={"url": "https://evil.com/p.png"}, timeout=5).status_code == 403
        assert (
            sess.get(f"{base_url}/api/image", params={"url": "http://127.0.0.1:8787/x.png"}, timeout=5).status_code
            == 403
        )
        assert (
            sess.get(f"{base_url}/api/image", params={"url": "http://media.trakt.tv/x.webp"}, timeout=5).status_code
            == 403
        )

        fake_img_resp = MagicMock()
        fake_img_resp.status_code = 200
        fake_img_resp.content = b"RIFF1234WEBP"
        with patch("webapp.server.requests.get", return_value=fake_img_resp) as mock_get:
            ok_img = sess.get(
                f"{base_url}/api/image",
                params={"url": "https://media.trakt.tv/movies/501/medium/poster.webp"},
                timeout=5,
            )
            assert ok_img.status_code == 200
            assert ok_img.content == b"RIFF1234WEBP"
            assert mock_get.call_count == 1
            # Second request hits local disk cache without calling requests.get again
            ok_img2 = sess.get(
                f"{base_url}/api/image",
                params={"url": "https://media.trakt.tv/movies/501/medium/poster.webp"},
                timeout=5,
            )
            assert ok_img2.status_code == 200
            assert mock_get.call_count == 1

        # 6. Partner want surfaces on partner's Together deck + direct /api/infuse/send + /api/qr
        sess.post(
            f"{base_url}/api/react",
            json={"user": "shelly", "trakt_id": 502, "media_type": "show", "reaction": "want"},
            timeout=5,
        )
        deck_mark = sess.get(
            f"{base_url}/api/deck",
            params={"mode": "together", "user": "mark"},
            timeout=5,
        ).json()
        partner_cards = [c for c in deck_mark["cards"] if c["trakt_id"] == 502]
        assert len(partner_cards) == 1
        assert partner_cards[0]["partner_wanted"] is True

        # Direct send for item already in collection returns ready immediately without calling add_to_watchlist
        mock_trakt.add_to_watchlist.reset_mock()
        mock_trakt.get_collection_and_watchlist_ids.return_value = ({(502, "show")}, set())
        direct_send = sess.post(
            f"{base_url}/api/infuse/send",
            json={"trakt_id": 502, "media_type": "show", "user": "shelly"},
            timeout=5,
        ).json()
        assert direct_send["ok"] is True
        assert direct_send["request"]["status"] == "ready"
        mock_trakt.add_to_watchlist.assert_not_called()

        # QR endpoint returns valid PNG
        qr_resp = sess.get(f"{base_url}/api/qr", timeout=5)
        assert qr_resp.status_code == 200
        assert qr_resp.content.startswith(b"\x89PNG")

        # 7. Browse romcoms endpoint returns 200
        rc_resp = sess.get(f"{base_url}/api/browse", params={"category": "romcoms", "user": "shelly"}, timeout=5)
        assert rc_resp.status_code == 200
        assert rc_resp.json()["category"] == "romcoms"
        assert len(rc_resp.json()["results"]) == 2


def test_trakt_romcoms_browse_filters_and_interleaves(tmp_path: Path) -> None:
    db = WatchNightDB(tmp_path / "rc.db")
    trakt = TraktClient()

    movies_payload = [
        {"ids": {"trakt": 1}, "title": "RomCom 1", "year": 2024, "genres": ["romance", "comedy"]},
        {"ids": {"trakt": 2}, "title": "Sad Drama", "year": 2024, "genres": ["romance", "drama"]},
        {"ids": {"trakt": 3}, "title": "RomCom 2", "year": 2023, "genres": ["comedy", "romance"]},
        {"ids": {"trakt": 4}, "title": "RomCom 3", "year": 2022, "genres": ["romance", "comedy"]},
        {"ids": {"trakt": 5}, "title": "RomCom 4", "year": 2021, "genres": ["romance", "comedy"]},
    ]
    shows_payload = [
        {"ids": {"trakt": 10}, "title": "Show Drama", "year": 2024, "genres": ["romance", "drama"]},
        {"ids": {"trakt": 11}, "title": "Nobody Wants This", "year": 2024, "genres": ["comedy", "romance"]},
    ]

    def fake_request(method, path, params=None, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/movies/popular":
            resp.json.return_value = movies_payload
        elif path == "/shows/popular":
            resp.json.return_value = shows_payload
        else:
            resp.json.return_value = []
        return resp

    with patch.object(trakt, "_request", side_effect=fake_request):
        items = trakt.get_browse_list("romcoms", limit=20, db=db)

    assert [(x["title"], x["media_type"]) for x in items] == [
        ("RomCom 1", "movie"),
        ("RomCom 2", "movie"),
        ("RomCom 3", "movie"),
        ("Nobody Wants This", "show"),
        ("RomCom 4", "movie"),
    ]
    assert db.get_media_item(11, "show") is not None


def test_ai_error_sanitizes_api_key() -> None:
    err_resp = MagicMock()
    err_resp.status_code = 400
    err_resp.json.return_value = {"error": {"message": "Bad key MY_SUPER_SECRET_KEY at ?key=MY_SUPER_SECRET_KEY"}}
    err_resp.text = "Bad key MY_SUPER_SECRET_KEY"
    with patch("webapp.ai_recommender.requests.post", return_value=err_resp):
        res = check_ai_connection("gemini", "gemini-3.8-flash", "MY_SUPER_SECRET_KEY")
        assert res["ok"] is False
        assert "MY_SUPER_SECRET_KEY" not in res["error"]
