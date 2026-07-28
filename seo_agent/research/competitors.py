"""Free competitor discovery via DuckDuckGo search results.

DuckDuckGo has no official free API for this, but the `ddgs` library
(formerly `duckduckgo-search`) scrapes its public HTML results without
requiring a key. This is a reasonable free stand-in for a paid SERP API
(SerpAPI/DataForSEO); results approximate but won't exactly match Google's
SERP.
"""
from urllib.parse import urlparse

from ddgs import DDGS


def find_competitors(seed_keyword: str, own_domain: str, max_results: int = 10) -> list[str]:
    own_netloc = urlparse(own_domain if "://" in own_domain else f"https://{own_domain}").netloc
    domains: list[str] = []

    with DDGS() as ddgs:
        for r in ddgs.text(seed_keyword, max_results=max_results):
            url = r.get("href") or r.get("link")
            if not url:
                continue
            netloc = urlparse(url).netloc
            if netloc and netloc != own_netloc and netloc not in domains:
                domains.append(netloc)

    return domains


def top_competitors_for_keywords(keywords: list[str], own_domain: str, top_n: int = 5) -> list[str]:
    from collections import Counter

    counter: Counter = Counter()
    for kw in keywords:
        for domain in find_competitors(kw, own_domain, max_results=10):
            counter[domain] += 1

    return [domain for domain, _ in counter.most_common(top_n)]
