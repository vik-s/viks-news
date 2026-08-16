#!/usr/bin/env python3
"""Build docs/feed.json: fetch sources, curate with Claude, merge, write.

Full run:      python scripts/build_feed.py        (needs OPENROUTER_API_KEY)
Source check:  python scripts/build_feed.py --dry-run   (no API call, no write)

Curation runs through OpenRouter by default. Set FEED_BACKEND=anthropic with
ANTHROPIC_API_KEY to use the Anthropic Messages API instead.

A failed run never touches the existing feed.json.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = ROOT / "docs" / "feed.json"
SOURCES_PATH = ROOT / "sources.txt"
PROMPT_PATH = ROOT / "prompt.md"

# Two backends behind one call_claude() signature:
#   openrouter — OpenRouter chat-completions API with OPENROUTER_API_KEY
#                (billed to the semidoped OpenRouter tokens, same as the
#                semidoped-news cloud runner and the EA jobs).
#   anthropic  — direct Anthropic Messages API with ANTHROPIC_API_KEY,
#                kept as an alternative metered path.
# Selection: FEED_BACKEND wins; otherwise whichever key is in the environment,
# OpenRouter first. The direct-Anthropic path is what silently broke this feed
# on 2026-07-31 (a payload-independent 400 from the Messages API), so both
# paths now surface the response body on failure.
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
# Model name → OpenRouter slug (verified against /api/v1/models).
OPENROUTER_MODELS = {
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
}
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "36"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "14"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "120"))
MAX_SEEN_URLS = 3000
FETCH_TIMEOUT = 30

TOPICS = ["optics", "memory", "packaging", "networking", "power", "ai-infra"]
TAGS = ["deep-dive", "digest", "fyi"]

SCHEMA_INSTRUCTIONS = f"""
OUTPUT FORMAT
Return ONLY a JSON array. No prose, no markdown fences.
Each element:
  {{"url": "<url copied exactly from a candidate>",
   "why": "<one sentence, max 40 words, on why this matters>",
   "topic": <one of {json.dumps(TOPICS)}>,
   "tag": <one of {json.dumps(TAGS)}>}}
Include only items worth attention per the brief above. Drop everything else.
An empty array [] is a valid answer.
"""


def log(msg):
    print(msg, flush=True)


def load_sources():
    sources = []
    for line in SOURCES_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, url = (p.strip() for p in line.split("|", 1))
        else:
            name, url = line, line
        sources.append({"name": name, "url": url})
    return sources


def strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_source(source):
    # Google News gets 18 requests per run; at a 4-hour cadence, space them
    # out so a burst never looks like scraping to their per-IP limiter.
    if "news.google.com" in source["url"]:
        time.sleep(2)
    resp = requests.get(
        source["url"],
        timeout=FETCH_TIMEOUT,
        # Browser-ish UA: several IR/newsroom hosts (Nokia among them) 403
        # anything that doesn't look like a browser.
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.bozo_exception}")
    return parsed.entries


def collect_candidates(sources, seen_set):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    candidates, statuses = [], []
    for source in sources:
        try:
            entries = fetch_source(source)
        except Exception as exc:
            log(f"FAIL  {source['name']}: {exc}")
            statuses.append({"name": source["name"], "ok": False, "count": 0})
            continue
        count = 0
        for entry in entries:
            url = (entry.get("link") or "").strip()
            title = strip_html(entry.get("title", ""))
            if not url or not title or url in seen_set:
                continue
            # Undated entries are dropped: a feed with no dates (Vertiv) would
            # otherwise dump its entire archive as "new" on every run.
            when = entry_time(entry)
            if not when or when < cutoff:
                continue
            candidates.append(
                {
                    "url": url,
                    "title": title[:200],
                    "source": source["name"],
                    "published": when.isoformat() if when else None,
                    "summary": strip_html(entry.get("summary", ""))[:300],
                }
            )
            count += 1
        statuses.append({"name": source["name"], "ok": True, "count": count})
        log(f"OK    {source['name']}: {count} new")
    # Round-robin across sources up to MAX_CANDIDATES: a straight [:MAX] cut
    # keeps the first N in sources.txt order, letting a few busy feeds at the
    # top of the file starve every source below them out of curation.
    by_source = {}
    for c in candidates:
        by_source.setdefault(c["source"], []).append(c)
    picked = []
    while len(picked) < MAX_CANDIDATES and by_source:
        for name in list(by_source):
            picked.append(by_source[name].pop(0))
            if not by_source[name]:
                del by_source[name]
            if len(picked) >= MAX_CANDIDATES:
                break
    return picked, statuses


def model_backend():
    """Which model backend to use. FEED_BACKEND overrides; otherwise pick by
    whichever key is present, OpenRouter first."""
    forced = os.environ.get("FEED_BACKEND", "").strip().lower()
    if forced in ("openrouter", "anthropic"):
        return forced
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else ""


def _fail(where, resp):
    """Raise with the response body attached. raise_for_status() discards it,
    which is what made the 2026-07-31 outage undiagnosable from the logs."""
    raise RuntimeError(f"{where} HTTP {resp.status_code}: {resp.text[:500]}")


def _call_openrouter(api_key, system, user_text):
    resp = requests.post(
        OPENROUTER_URL,
        timeout=240,
        json={
            "model": OPENROUTER_MODELS.get(MODEL, MODEL),
            "max_tokens": 8000,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "viks-news",
        },
    )
    if resp.status_code != 200:
        _fail("OpenRouter", resp)
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter 200 without choices: {str(data)[:300]}")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("OpenRouter response truncated at 8000 tokens")
    return (choice.get("message", {}).get("content") or "").strip()


def _call_anthropic(api_key, system, user_text):
    resp = requests.post(
        API_URL,
        timeout=240,
        json={
            "model": MODEL,
            "max_tokens": 8000,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        },
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
    )
    if resp.status_code != 200:
        _fail("Anthropic", resp)
    data = resp.json()
    return "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )


def call_claude(api_key, system, user_text):
    if model_backend() == "openrouter":
        return _call_openrouter(api_key, system, user_text)
    return _call_anthropic(api_key, system, user_text)


def parse_json_array(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in response")
    return json.loads(text[start : end + 1])


def curate(candidates, api_key):
    system = PROMPT_PATH.read_text() + "\n" + SCHEMA_INSTRUCTIONS
    user_text = "Candidates:\n" + json.dumps(candidates)
    text = call_claude(api_key, system, user_text)
    try:
        raw = parse_json_array(text)
    except (ValueError, json.JSONDecodeError):
        log("Response was not valid JSON; retrying once")
        text = call_claude(
            api_key,
            system,
            user_text + "\n\nYour previous reply was not valid JSON. "
            "Reply with ONLY the JSON array.",
        )
        raw = parse_json_array(text)
    by_url = {c["url"]: c for c in candidates}
    kept = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cand = by_url.get(str(item.get("url", "")).strip())
        if not cand:
            continue
        kept.append(
            {
                "url": cand["url"],
                "title": cand["title"],
                "source": cand["source"],
                "published": cand["published"],
                "why": str(item.get("why", "")).strip()[:300],
                "topic": item["topic"] if item.get("topic") in TOPICS else "ai-infra",
                "tag": item["tag"] if item.get("tag") in TAGS else "fyi",
            }
        )
    return kept


def load_feed():
    if FEED_PATH.exists():
        try:
            return json.loads(FEED_PATH.read_text())
        except json.JSONDecodeError:
            log("Existing feed.json unreadable; starting fresh")
    return {"meta": {}, "items": [], "seen_urls": []}


def write_feed(feed):
    tmp = FEED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(feed, indent=1))
    os.replace(tmp, FEED_PATH)


def main():
    dry_run = "--dry-run" in sys.argv
    sources = load_sources()
    feed = load_feed()

    seen_list = list(feed.get("seen_urls", []))
    seen_set = set(seen_list)
    for item in feed.get("items", []):
        seen_set.add(item["url"])

    candidates, statuses = collect_candidates(sources, seen_set)
    ok = [s for s in statuses if s["ok"]]
    log(f"Sources: {len(ok)}/{len(statuses)} ok, {len(candidates)} new candidates")

    if not ok:
        log("Every source failed; leaving feed untouched")
        return 1

    if dry_run:
        for c in candidates:
            log(f"  [{c['source']}] {c['title']}")
        return 0

    backend = model_backend()
    kept = []
    if candidates:
        if not backend:
            log("No model key set (want OPENROUTER_API_KEY, or ANTHROPIC_API_KEY)")
            return 1
        key_var = (
            "OPENROUTER_API_KEY" if backend == "openrouter" else "ANTHROPIC_API_KEY"
        )
        api_key = os.environ.get(key_var)
        if not api_key:
            log(f"FEED_BACKEND={backend} but {key_var} is not set")
            return 1
        log(f"Curating {len(candidates)} candidates via {backend} ({MODEL})")
        kept = curate(candidates, api_key)
    log(f"Kept {len(kept)} of {len(candidates)}")

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    for item in kept:
        item["date"] = today
        item["added_at"] = now.isoformat()

    cutoff = now - timedelta(days=KEEP_DAYS)
    old_items = [
        i
        for i in feed.get("items", [])
        if datetime.fromisoformat(i["added_at"]) >= cutoff
    ]

    for c in candidates:
        if c["url"] not in set(seen_list):
            seen_list.append(c["url"])

    write_feed(
        {
            "meta": {
                "generated_at": now.isoformat(),
                "model": MODEL,
                "backend": backend,
                "sources": statuses,
                "candidates": len(candidates),
                "kept": len(kept),
            },
            "items": kept + old_items,
            "seen_urls": seen_list[-MAX_SEEN_URLS:],
        }
    )
    log(f"Wrote {FEED_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
