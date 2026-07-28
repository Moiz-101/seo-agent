"""Free keyword research via Google Trends (pytrends).

Note: pytrends gives relative interest + related/rising queries, not exact
search volume (that needs a paid tool or a Google Ads Keyword Planner
account). For a free pipeline this is the best available signal.
"""
import time

from pytrends.request import TrendReq


def _related_queries_with_retry(pytrends: TrendReq, topic: str, geo: str, attempts: int = 3) -> dict:
    """pytrends occasionally 429s / returns empty on the first call of a
    session (Google Trends anti-bot quirk). Retrying once or twice usually
    succeeds without needing any paid API.
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
        time.sleep(3 * (attempt + 1))

    if last_error:
        raise last_error
    return {}


def research_keywords(seed_topics: list[str], geo: str = "IN") -> list[dict]:
    """Returns a list of {keyword, source, signal} dicts, deduped."""
    pytrends = TrendReq(hl="en-US", tz=330)
    found: dict[str, dict] = {}

    for topic in seed_topics:
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
        except Exception:
            # still flaky after retries; skip this topic rather than crash the whole pipeline
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
