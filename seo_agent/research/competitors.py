"""Free competitor discovery via DuckDuckGo search results.

DuckDuckGo has no official free API for this, but the `ddgs` library
(formerly `duckduckgo-search`) scrapes its public HTML results without
requiring a key. This is a reasonable free stand-in for a paid SERP API
(SerpAPI/DataForSEO); results approximate but won't exactly match Google's
SERP.
"""
from collections import defaultdict
from urllib.parse import urlparse

from ddgs import DDGS

# Sites that show up in SERPs but aren't a business's actual competitor -
# B2B listing/review directories, social platforms, and generic reference
# sites. Never label these as "direct competitors" in a report.
DIRECTORY_DOMAINS = {
    "clutch.co", "goodfirms.co", "upcity.com", "designrush.com", "sortlist.com",
    "topseos.com", "expertise.com", "thomasnet.com", "yellowpages.com",
    "yelp.com", "tripadvisor.com", "trustpilot.com", "bbb.org", "angi.com",
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
    "pinterest.com", "tiktok.com", "twitter.com", "x.com", "maps.google.com",
    "google.com", "amazon.com", "ebay.com",
}
INFORMATIONAL_DOMAINS = {"wikipedia.org", "quora.com", "reddit.com", "medium.com"}


def classify_domain(domain: str) -> str:
    bare = domain.lower()
    if bare.startswith("www."):
        bare = bare[4:]
    if any(bare == d or bare.endswith("." + d) for d in DIRECTORY_DOMAINS):
        return "Directory/Aggregator"
    if any(bare == d or bare.endswith("." + d) for d in INFORMATIONAL_DOMAINS) or bare.endswith((".gov", ".edu")):
        return "Informational"
    return "Organic SERP Competitor"


def find_competitors_classified(seed_keywords: list[str], own_domain: str, max_results_per_kw: int = 10) -> list[dict]:
    """Runs each seed keyword through DuckDuckGo search and classifies every
    domain found. A domain showing up for 2+ different keywords is upgraded
    from "Organic SERP Competitor" to "Direct Business Competitor" - a
    frequency-based proxy for relevance, since there's no free tool that
    verifies true competitive overlap the way a paid SERP-tracking API would.
    Directories/aggregators (Clutch, GoodFirms, social platforms, etc.) and
    informational sites are never classified as direct competitors.
    """
    own_netloc = urlparse(own_domain if "://" in own_domain else f"https://{own_domain}").netloc
    domain_hits: dict[str, int] = defaultdict(int)
    domain_category: dict[str, str] = {}

    with DDGS() as ddgs:
        for kw in seed_keywords:
            try:
                for r in ddgs.text(kw, max_results=max_results_per_kw):
                    url = r.get("href") or r.get("link")
                    if not url:
                        continue
                    netloc = urlparse(url).netloc
                    if not netloc or netloc == own_netloc:
                        continue
                    domain_hits[netloc] += 1
                    domain_category.setdefault(netloc, classify_domain(netloc))
            except Exception:
                continue

    results = []
    for domain, hits in domain_hits.items():
        category = domain_category[domain]
        if category == "Organic SERP Competitor" and hits >= 2:
            category = "Direct Business Competitor"
        results.append({"domain": domain, "category": category, "keyword_matches": hits})

    results.sort(key=lambda r: r["keyword_matches"], reverse=True)
    return results


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
