"""Multi-provider AI recommendation engine + free Trakt fallback for WatchNight."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import requests

from webapp.config import AI_MODEL_PRESETS
from webapp.db import WatchNightDB, normalize_title
from webapp.trakt_client import TraktClient

SYSTEM_PROMPT = (
    "You are WatchNight, a warm, perceptive movie & TV curator for a couple named Mark and Shelly. "
    "You specialize in spotting tonal overlap, pacing, humor, and storytelling hooks that satisfy "
    "two viewers watching together—or tailoring picks for one viewer when requested. "
    "Always return valid JSON only with no extra commentary."
)


def _sanitize_error(msg: str, api_key: str) -> str:
    text = str(msg or "Unknown error")
    if api_key:
        text = text.replace(api_key, "***")
    text = re.sub(r"([?&]key=)[^&\s]+", r"\1***", text)
    return text


def parse_ai_json(raw_text: str) -> list[dict[str, Any]]:
    """Parse JSON from an LLM response, gracefully stripping markdown code fences."""
    text = (raw_text or "").strip()
    if not text:
        return []

    # Strip ```json ... ``` or ``` ... ``` fences
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()

    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Try extracting outermost JSON object or array
        obj_start = text.find("{")
        obj_end = text.rfind("}")
        arr_start = text.find("[")
        arr_end = text.rfind("]")
        candidates = []
        if obj_start != -1 and obj_end > obj_start:
            candidates.append(text[obj_start : obj_end + 1])
        if arr_start != -1 and arr_end > arr_start:
            candidates.append(text[arr_start : arr_end + 1])
        for cand in candidates:
            try:
                parsed = json.loads(cand)
                break
            except json.JSONDecodeError:
                continue

    if isinstance(parsed, dict):
        recs = parsed.get("recommendations") or parsed.get("picks") or parsed.get("items") or []
    elif isinstance(parsed, list):
        recs = parsed
    else:
        return []

    cleaned: list[dict[str, Any]] = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        mt = str(r.get("media_type") or r.get("type") or "").strip().lower()
        if mt in ("tv", "series", "tv show"):
            mt = "show"
        elif mt in ("film",):
            mt = "movie"
        if mt not in ("movie", "show"):
            mt = ""
        year_val = r.get("year")
        try:
            year_int = int(year_val) if year_val else None
        except (TypeError, ValueError):
            year_int = None
        tags = r.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        cleaned.append(
            {
                "title": title,
                "year": year_int,
                "media_type": mt,
                "pitch": str(r.get("pitch") or r.get("reason") or "").strip(),
                "tags": [str(t).strip() for t in tags[:4] if str(t).strip()],
            }
        )
    return cleaned


def build_taste_prompt(
    db: WatchNightDB,
    config: dict[str, Any],
    mode: str = "together",
    mood_prompt: str = "",
    media_filter: str = "both",
    candidate_count: int = 22,
) -> str:
    """Build a structured taste & exclusion prompt from SQLite + user notes."""
    summary = db.get_taste_summary_for_ai()
    notes = config.get("taste_notes") or {}
    matches = db.get_mutual_matches()
    match_titles = [f"{m['title']} ({m.get('year') or '?'})" for m in matches[:15]]

    if mode == "shelly":
        goal = (
            "Recommend titles specifically tailored for SHELLY to enjoy (movies/shows she will genuinely get hooked on)."
        )
    elif mode == "mark":
        goal = "Recommend titles specifically tailored for MARK based on his watch and rating patterns."
    else:
        goal = (
            "Recommend titles for MARK AND SHELLY TO WATCH TOGETHER — look for the sweet spot where "
            "both of their tastes overlap, and explain in each pitch why BOTH of them will enjoy it."
        )

    if media_filter == "movie":
        type_rule = "ONLY recommend MOVIES (media_type: 'movie')."
    elif media_filter == "show":
        type_rule = "ONLY recommend TV SHOWS or MINISERIES (media_type: 'show')."
    else:
        type_rule = "Include a balanced mix of BOTH movies ('movie') and TV shows/miniseries ('show')."

    mood_section = (
        f"\nREQUESTED MOOD / VIBE FOR THIS SESSION:\n\"{mood_prompt.strip()}\"\n"
        if (mood_prompt or "").strip()
        else "\nSESSION VIBE: General date-night / great-watch discovery (varied genres, strong pacing, high quality).\n"
    )

    def fmt_lines(items: list[str], empty_msg: str) -> str:
        return "\n".join(f"- {x}" for x in items) if items else f"- ({empty_msg})"

    prompt = f"""GOAL: {goal}
MEDIA TYPE CONSTRAINT: {type_rule}
{mood_section}
SHELLY'S TASTE PROFILE (Netflix / Prime / Hulu & in-app likes):
Notes about Shelly: {notes.get('shelly') or 'None added yet.'}
In-app favorites / loved:
{fmt_lines(summary['shelly_likes'], 'None picked yet — rely on Netflix history, notes, or accessible crossover appeal')}
In-app wants to watch:
{fmt_lines(summary['shelly_wants'], 'None')}
Netflix watch history (top/recent):
{fmt_lines(summary['shelly_netflix'], 'None imported yet')}
Shelly passed / disliked:
{fmt_lines(summary['shelly_dislikes'], 'None')}

MARK'S TASTE PROFILE (Trakt / Infuse history & ratings):
Notes about Mark: {notes.get('mark') or 'None added yet.'}
Top rated on Trakt & loved in-app:
{fmt_lines(summary['mark_likes'], 'No ratings yet')}
Most-watched TV shows:
{fmt_lines(summary['mark_top_shows'], 'None')}
Recent watches:
{fmt_lines(summary['mark_recent'], 'None')}
In-app wants:
{fmt_lines(summary['mark_wants'], 'None')}
Mark passed / low-rated:
{fmt_lines(summary['mark_dislikes'], 'None')}

SHARED COUPLE NOTES & MUTUAL MATCHES:
Notes for watching together: {notes.get('together') or 'None added yet.'}
Mutual matches already queued/picked: {', '.join(match_titles) if match_titles else 'None yet'}

DO NOT RECOMMEND ANY OF THESE ALREADY WATCHED / QUEUED / PASSED TITLES:
{', '.join(summary['avoid_sample']) if summary['avoid_sample'] else 'None'}

INSTRUCTIONS:
1. Return EXACTLY {candidate_count} diverse, high-signal recommendations (mix standout hidden gems with acclaimed favorites — not just the same 5 obvious titles).
2. Avoid everything in the DO NOT RECOMMEND list and any obvious remakes/seasons of shows they've already watched.
3. For each pick, write a crisp 1-2 sentence `pitch` explaining specifically why it fits (and for Together mode, name the hook for both Mark and Shelly).
4. Respond ONLY with a JSON object matching this exact schema:
{{
  "recommendations": [
    {{
      "title": "Exact Official Title",
      "year": 2023,
      "media_type": "show",
      "pitch": "1-2 concise sentences tailored to Mark & Shelly.",
      "tags": ["Tag 1", "Tag 2"]
    }}
  ]
}}"""
    return prompt


def _call_gemini(model: str, api_key: str, system_prompt: str, user_prompt: str, timeout: int = 45) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.85,
        },
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "") or resp.text[:300]
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(f"Gemini API error ({resp.status_code}): {_sanitize_error(detail, api_key)}")
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates.")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    return "".join(str(p.get("text") or "") for p in parts)


def _call_openai(model: str, api_key: str, system_prompt: str, user_prompt: str, timeout: int = 45) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "temperature": 0.85,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "") or resp.text[:300]
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(f"OpenAI API error ({resp.status_code}): {_sanitize_error(detail, api_key)}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI returned no choices.")
    return str(((choices[0].get("message") or {}).get("content")) or "")


def _call_anthropic(model: str, api_key: str, system_prompt: str, user_prompt: str, timeout: int = 45) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 4096,
        "temperature": 0.85,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "") or resp.text[:300]
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(f"Anthropic API error ({resp.status_code}): {_sanitize_error(detail, api_key)}")
    data = resp.json()
    content = data.get("content") or []
    texts = [str(block.get("text") or "") for block in content if block.get("type") == "text"]
    return "".join(texts)


def call_provider(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 45,
) -> str:
    p = (provider or "gemini").lower()
    if p == "gemini":
        return _call_gemini(model, api_key, system_prompt, user_prompt, timeout=timeout)
    if p == "openai":
        return _call_openai(model, api_key, system_prompt, user_prompt, timeout=timeout)
    if p == "anthropic":
        return _call_anthropic(model, api_key, system_prompt, user_prompt, timeout=timeout)
    raise ValueError(f"Unsupported AI provider: {provider}")


def test_ai_connection(provider: str, model: str, api_key: str) -> dict[str, Any]:
    """Send a lightweight ping to validate provider, model, and API key."""
    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "error": "Please enter an API key first."}
    prov = (provider or "gemini").lower()
    mod = (model or "").strip() or AI_MODEL_PRESETS.get(prov, {}).get("default", "")
    start = time.time()
    try:
        raw = call_provider(
            prov,
            mod,
            key,
            "Return valid JSON only.",
            'Reply with {"status":"ok","note":"ready"}',
            timeout=20,
        )
        latency_ms = int((time.time() - start) * 1000)
        return {
            "ok": True,
            "provider": prov,
            "model": mod,
            "latency_ms": latency_ms,
            "preview": raw[:120],
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": prov,
            "model": mod,
            "error": _sanitize_error(str(exc), key),
        }


def generate_trakt_fallback(
    db: WatchNightDB,
    trakt: TraktClient,
    mode: str = "together",
    user: str = "shelly",
    media_filter: str = "both",
    mood_prompt: str = "",
    count: int = 12,
) -> dict[str, Any]:
    """Generate recommendations using Trakt related + trending/popular lists without an AI key."""
    excluded_keys, excluded_titles = db.get_exclusion_sets(mode=mode, user=user)

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    # 1. Seed related titles from user's loved/wanted items or top-rated Trakt items
    with db._connect() as conn:
        seed_rows = conn.execute(
            """
            SELECT r.trakt_id, r.media_type, m.title
            FROM reactions r
            JOIN media_items m ON m.trakt_id = r.trakt_id AND m.media_type = r.media_type
            WHERE r.reaction IN ('seen_loved', 'want')
            ORDER BY r.updated_at DESC
            LIMIT 4
            """
        ).fetchall()
        if not seed_rows:
            seed_rows = conn.execute(
                """
                SELECT th.trakt_id, th.media_type, m.title
                FROM trakt_history th
                JOIN media_items m ON m.trakt_id = th.trakt_id AND m.media_type = th.media_type
                WHERE th.source = 'rating' AND th.user_rating >= 8
                ORDER BY th.user_rating DESC, th.last_watched_at DESC
                LIMIT 3
                """
            ).fetchall()

    for seed in seed_rows[:3]:
        s_mt = seed["media_type"]
        if media_filter in ("movie", "show") and s_mt != media_filter:
            continue
        try:
            rel_items = trakt.get_related(int(seed["trakt_id"]), s_mt, limit=8, db=db)
            for item in rel_items:
                key = (int(item["trakt_id"]), item["media_type"])
                if key in seen:
                    continue
                seen.add(key)
                item_copy = dict(item)
                genres = ", ".join((item.get("genres") or [])[:2]).title()
                rating_txt = f"{item['rating']:.1f}/10" if item.get("rating") else "Acclaimed"
                item_copy["pitch"] = (
                    f"Because you liked {seed['title']} — {genres or 'great match'} ({rating_txt} on Trakt)."
                )
                item_copy["tags"] = [g.title() for g in (item.get("genres") or [])[:2]] or ["Similar Taste"]
                candidates.append(item_copy)
        except Exception:
            pass

    # 2. Fill from trending & popular lists
    categories = []
    if media_filter in ("both", "show"):
        categories.extend(["trending_shows", "popular_shows"])
    if media_filter in ("both", "movie"):
        categories.extend(["trending_movies", "popular_movies"])

    for cat in categories:
        try:
            items = trakt.get_browse_list(cat, limit=20, db=db)
            is_trending = cat.startswith("trending")
            for item in items:
                key = (int(item["trakt_id"]), item["media_type"])
                if key in seen:
                    continue
                seen.add(key)
                item_copy = dict(item)
                genres = ", ".join((item.get("genres") or [])[:2]).title()
                rating_txt = f"{item['rating']:.1f}/10" if item.get("rating") else "Popular"
                label = "Trending now" if is_trending else "Viewer favorite"
                kind = "series" if item["media_type"] == "show" else "movie"
                item_copy["pitch"] = f"{label} {kind}: {genres or 'critically well-reviewed'} ({rating_txt} on Trakt)."
                item_copy["tags"] = [g.title() for g in (item.get("genres") or [])[:2]] or [label]
                candidates.append(item_copy)
        except Exception:
            pass

    mood_words = [w.lower() for w in re.findall(r"[a-zA-Z]{3,}", mood_prompt or "")]

    def score_item(item: dict[str, Any]) -> float:
        base = float(item.get("rating") or 6.5)
        if "Similar Taste" in (item.get("tags") or []) or str(item.get("pitch") or "").startswith("Because"):
            base += 2.0
        if mood_words:
            blob = f"{item.get('title','')} {item.get('overview','')} {' '.join(item.get('genres') or [])}".lower()
            base += sum(1.2 for w in mood_words if w in blob)
        return base

    filtered: list[dict[str, Any]] = []
    for item in candidates:
        key = (int(item["trakt_id"]), item["media_type"])
        if key in excluded_keys:
            continue
        if normalize_title(item.get("title")) in excluded_titles:
            continue
        item["score"] = score_item(item)
        filtered.append(item)

    filtered.sort(key=lambda x: x["score"], reverse=True)
    selected = filtered[:count]

    batch_id = uuid.uuid4().hex[:10]
    db.save_recommendations(
        mode=mode,
        mood_prompt=mood_prompt,
        items=selected,
        source="trakt_fallback",
        batch_id=batch_id,
        clear_previous_mode=True,
    )
    return {
        "source": "trakt_fallback",
        "batch_id": batch_id,
        "count": len(selected),
        "cards": db.get_deck(mode=mode, user=user, media_type=media_filter, limit=count),
    }


def generate_recommendations(
    db: WatchNightDB,
    trakt: TraktClient,
    config: dict[str, Any],
    mode: str = "together",
    user: str = "shelly",
    mood_prompt: str = "",
    media_filter: str = "both",
    count: int = 12,
) -> dict[str, Any]:
    """Generate fresh recommendations via configured AI provider, or fallback to Trakt."""
    api_key = str(config.get("ai_api_key") or "").strip()
    provider = str(config.get("ai_provider") or "gemini").lower()
    model = str(config.get("ai_model") or AI_MODEL_PRESETS.get(provider, {}).get("default", ""))

    if not api_key:
        res = generate_trakt_fallback(
            db=db,
            trakt=trakt,
            mode=mode,
            user=user,
            media_filter=media_filter,
            mood_prompt=mood_prompt,
            count=count,
        )
        res["warning"] = "Using smart Trakt picks (add an AI key in Settings for personalized couple AI picks)."
        return res

    excluded_keys, excluded_titles = db.get_exclusion_sets(mode=mode, user=user)
    prompt = build_taste_prompt(
        db=db,
        config=config,
        mode=mode,
        mood_prompt=mood_prompt,
        media_filter=media_filter,
        candidate_count=22,
    )

    try:
        raw = call_provider(provider, model, api_key, SYSTEM_PROMPT, prompt, timeout=45)
        parsed = parse_ai_json(raw)
        if not parsed:
            raise RuntimeError("AI response did not contain parsable recommendations.")
    except Exception as exc:
        res = generate_trakt_fallback(
            db=db,
            trakt=trakt,
            mode=mode,
            user=user,
            media_filter=media_filter,
            mood_prompt=mood_prompt,
            count=count,
        )
        res["warning"] = f"AI unavailable ({_sanitize_error(str(exc), api_key)}); showing Trakt picks instead."
        return res

    verified: list[dict[str, Any]] = []
    seen_keys: set[tuple[int, str]] = set()
    for rec in parsed:
        if media_filter in ("movie", "show") and rec.get("media_type") and rec["media_type"] != media_filter:
            continue
        target_mt = media_filter if media_filter in ("movie", "show") else (rec.get("media_type") or None)
        try:
            resolved = trakt.resolve_title(
                rec["title"],
                year=rec.get("year"),
                media_type=target_mt,
                db=db,
            )
        except Exception:
            continue
        time.sleep(0.08)
        if not resolved or not resolved.get("trakt_id"):
            continue
        mt = resolved["media_type"]
        if media_filter in ("movie", "show") and mt != media_filter:
            continue
        key = (int(resolved["trakt_id"]), mt)
        if key in seen_keys or key in excluded_keys:
            continue
        if normalize_title(resolved.get("title")) in excluded_titles:
            continue

        seen_keys.add(key)
        merged = dict(resolved)
        merged["pitch"] = rec.get("pitch") or merged.get("tagline") or merged.get("overview", "")[:160]
        merged["tags"] = rec.get("tags") or [g.title() for g in (merged.get("genres") or [])[:2]]
        merged["score"] = float(100 - len(verified))
        verified.append(merged)
        if len(verified) >= count:
            break

    warning = None
    if not verified:
        res = generate_trakt_fallback(
            db=db,
            trakt=trakt,
            mode=mode,
            user=user,
            media_filter=media_filter,
            mood_prompt=mood_prompt,
            count=count,
        )
        res["warning"] = "AI picks were already watched or unresolved; showing fresh Trakt picks."
        return res

    batch_id = uuid.uuid4().hex[:10]
    db.save_recommendations(
        mode=mode,
        mood_prompt=mood_prompt,
        items=verified,
        source=f"ai:{provider}",
        batch_id=batch_id,
        clear_previous_mode=True,
    )
    return {
        "source": f"ai:{provider}",
        "model": model,
        "batch_id": batch_id,
        "count": len(verified),
        "warning": warning,
        "cards": db.get_deck(mode=mode, user=user, media_type=media_filter, limit=count),
    }
