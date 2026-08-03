"""Free keyword research combining two sources:

1. Google Trends (pytrends) - related/rising queries with a relative
   interest signal. Best-effort: Google aggressively 429s requests coming
   from cloud/datacenter IPs (Render, AWS, etc.), so this frequently
   returns nothing when deployed, even though it works fine from a home
   network.
2. DuckDuckGo autocomplete - real search-suggestion data, no API key,
   and (unlike Trends) has been reliable from cloud IPs in testing. This
   is the primary source; Trends is a bonus when it isn't blocked.

Neither gives exact search volume (that needs a paid tool or a Google Ads
Keyword Planner account) but together they're a solid free signal.
"""
import logging
import re
import time

import requests
from pytrends.request import TrendReq

logger = logging.getLogger(__name__)

AUTOCOMPLETE_URL = "https://duckduckgo.com/ac/"


def autocomplete_only(topic: str) -> list[dict]:
    """Fast, reliable keyword suggestions for a topic - skips Google Trends
    entirely (used by the audit report, where we don't want to wait on
    Trends' slow retries/429s just to fill a 'keyword opportunities' box).
    """
    return _autocomplete_keywords(topic)


def _autocomplete_keywords(topic: str) -> list[dict]:
    try:
        resp = requests.get(AUTOCOMPLETE_URL, params={"q": topic, "type": "list"}, timeout=10)
        resp.raise_for_status()
        _, suggestions = resp.json()
    except Exception as e:
        logger.warning("DuckDuckGo autocomplete failed for topic %r: %s", topic, e)
        return []

    results = []
    for i, suggestion in enumerate(suggestions):
        kw = suggestion.strip().lower()
        if not kw or kw == topic.lower():
            continue
        if re.search(r"\d{3,}", kw):
            # DuckDuckGo's suggestions occasionally include noise like zip
            # codes ("tucson 85719 acura repair") mixed in with genuine
            # keyword phrases - a 3+ digit run is a reliable signal of that,
            # since real long-tail SEO phrases essentially never contain one.
            continue
        results.append({"keyword": kw, "source": "duckduckgo_autocomplete", "signal": len(suggestions) - i})
    return results


def _related_queries_with_retry(pytrends: TrendReq, topic: str, geo: str, attempts: int = 2) -> dict:
    """pytrends occasionally 429s / returns empty on the first call of a
    session. Retrying once usually helps on residential networks; on
    cloud IPs it's often blocked outright, so we don't retry aggressively
    here since DuckDuckGo autocomplete is the reliable fallback anyway.
    """
    last_error = None
    for attempt in range(attempts):
        try:
            pytrends.build_payload([topic], geo=geo, timeframe="today 12-m")
            related = pytrends.related_queries()
            topic_data = related.get(topic, {})
            if topic_data.get("top") is not None or topic_data.get("rising") is not None:
                return topic_data
        except Exception as e:
            last_error = e
        time.sleep(2 * (attempt + 1))

    if last_error:
        raise last_error
    return {}


def research_keywords(seed_topics: list[str], geo: str = "IN") -> list[dict]:
    """Returns a list of {keyword, source, signal} dicts, deduped."""
    pytrends = TrendReq(hl="en-US", tz=330)
    found: dict[str, dict] = {}

    for topic in seed_topics:
        for kw_data in _autocomplete_keywords(topic):
            if kw_data["keyword"] not in found:
                found[kw_data["keyword"]] = kw_data

        try:
            topic_data = _related_queries_with_retry(pytrends, topic, geo)

            for kind in ("top", "rising"):
                df = topic_data.get(kind)
                if df is None:
                    continue
                for _, row in df.iterrows():
                    kw = str(row["query"]).strip().lower()
                    if kw not in found:
                        found[kw] = {
                            "keyword": kw,
                            "source": f"google_trends_{kind}",
                            "signal": float(row.get("value", 0)),
                        }
        except Exception as e:
            logger.warning("Google Trends lookup failed for topic %r: %s", topic, e)
            continue

    return sorted(found.values(), key=lambda k: k["signal"], reverse=True)


def cluster_keywords(keywords: list[dict], max_clusters: int = 8) -> list[list[dict]]:
    """Very simple clustering: group by shared significant word.

    Good enough for turning ~50-100 keywords into a content calendar without
    needing a paid NLP/clustering API.
    """
    import re
    from collections import defaultdict

    STOPWORDS = {"the", "a", "an", "for", "of", "in", "on", "to", "and", "best", "how"}
    groups: dict[str, list[dict]] = defaultdict(list)

    for kw in keywords:
        words = [w for w in re.findall(r"[a-z0-9]+", kw["keyword"]) if w not in STOPWORDS]
        key = words[0] if words else kw["keyword"]
        groups[key].append(kw)

    clusters = sorted(groups.values(), key=len, reverse=True)
    return clusters[:max_clusters]
