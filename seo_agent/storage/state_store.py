import json
import os
from datetime import datetime

from config import DATA_DIR, UPSTASH_REDIS_REST_TOKEN, UPSTASH_REDIS_REST_URL

STATE_FILE = os.path.join(DATA_DIR, "sites_state.json")
REDIS_KEY = "seo_agent:sites_state"

_redis = None
if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN:
    from upstash_redis import Redis

    _redis = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)


def _load_all() -> dict:
    if _redis:
        raw = _redis.get(REDIS_KEY)
        return json.loads(raw) if raw else {}

    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_all(data: dict) -> None:
    if _redis:
        _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False))
        return

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_site(url: str) -> dict | None:
    return _load_all().get(url)


def create_site(url: str) -> dict:
    data = _load_all()
    data[url] = {
        "url": url,
        "stage": "AUDITING",
        "keywords": [],
        "competitors": [],
        "content_queue": [],
        "published_topics": [],
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        "active": True,
    }
    _save_all(data)
    return data[url]


def update_site(url: str, **fields) -> dict:
    data = _load_all()
    if url not in data:
        raise KeyError(f"Site {url} not tracked yet")
    data[url].update(fields)
    data[url]["updated_at"] = datetime.utcnow().isoformat()
    _save_all(data)
    return data[url]


def get_active_site_for_user() -> dict | None:
    """Single-user bot: returns the most recently updated active site."""
    data = _load_all()
    active = [s for s in data.values() if s.get("active")]
    if not active:
        return None
    return sorted(active, key=lambda s: s["updated_at"])[-1]


def stop_site(url: str) -> None:
    update_site(url, active=False, stage="STOPPED")


def list_sites() -> list[dict]:
    return list(_load_all().values())
