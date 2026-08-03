"""Free competitor discovery via DuckDuckGo search results.

DuckDuckGo has no official free API for this, but the `ddgs` library
(formerly `duckduckgo-search`) scrapes its public HTML results without
requiring a key. This is a reasonable free stand-in for a paid SERP API
(SerpAPI/DataForSEO); results approximate but won't exactly match Google's
SERP.
"""
import logging
from collections import defaultdict
from urllib.parse import urlparse

import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)
genai.configure(api_key=GEMINI_API_KEY)

# Sites that show up in SERPs but aren't a business's actual competitor -
# B2B listing/review directories, SEO/marketing tool vendors, social
# platforms, and generic reference sites. Never label these as "direct
# competitors" in a report.
DIRECTORY_DOMAINS = {
    "clutch.co", "goodfirms.co", "upcity.com", "designrush.com", "sortlist.com",
    "topseos.com", "expertise.com", "thomasnet.com", "yellowpages.com",
    "yelp.com", "tripadvisor.com", "trustpilot.com", "bbb.org", "angi.com",
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
    "pinterest.com", "tiktok.com", "twitter.com", "x.com", "threads.net",
    "maps.google.com", "google.com", "amazon.com", "ebay.com",
    "semrush.com", "ahrefs.com", "moz.com", "similarweb.com", "capterra.com",
    "g2.com", "trustradius.com", "getapp.com", "softwareadvice.com",
    "crunchbase.com", "glassdoor.com", "indeed.com", "manifest.ly", "themanifest.com",
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
    Directories/aggregators (Clutch, GoodFirms, social platforms, SEO tool
    vendors, etc.) and informational sites are never classified as direct
    competitors. Call verify_direct_competitors() afterwards to spot-check
    the top candidates with an LLM read of their homepage.
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


def _fetch_homepage_summary(domain: str) -> str | None:
    try:
        resp = requests.get(f"https://{domain}", timeout=8, headers={"User-Agent": "Mozilla/5.0 (SEO-Agent)"})
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        meta = soup.find("meta", attrs={"name": "description"})
        desc = meta["content"].strip() if meta and meta.get("content") else ""
        return f"{title} - {desc}".strip(" -") or None
    except Exception:
        return None


def verify_direct_competitors(competitors: list[dict], own_business_summary: str, max_verify: int = 8) -> list[dict]:
    """Spot-checks the top 'Direct Business Competitor' candidates by
    fetching their homepage and asking Gemini whether it looks like a
    genuinely similar, independent commercial business (vs. a directory,
    marketplace, tool vendor, or unrelated site the domain blocklist
    missed). Downgrades anything that doesn't pass to 'Organic SERP
    Competitor' rather than trusting the frequency heuristic alone.
    """
    if not GEMINI_API_KEY:
        return competitors

    candidates = [c for c in competitors if c["category"] == "Direct Business Competitor"][:max_verify]
    if not candidates:
        return competitors

    model = genai.GenerativeModel("gemini-flash-latest")
    verified_domains = set()

    for c in candidates:
        summary = _fetch_homepage_summary(c["domain"])
        if not summary:
            continue  # can't verify - leave classification as-is rather than guessing
        try:
            prompt = (
                f"A business is described as: \"{own_business_summary}\".\n"
                f"A site found in its search results has this homepage title/description: \"{summary}\" "
                f"(domain: {c['domain']}).\n"
                "Is this site a genuinely similar, independent commercial business offering similar "
                "services in the same space (not a directory, marketplace, review site, social "
                "platform, or SEO/marketing tool vendor)? Answer with exactly one word: yes, no, or unclear."
            )
            response = model.generate_content(prompt)
            answer = response.text.strip().lower()
            if answer.startswith("yes"):
                verified_domains.add(c["domain"])
        except Exception as e:
            logger.warning("Competitor verification failed for %s: %s", c["domain"], e)

    updated = []
    for c in competitors:
        if c["category"] == "Direct Business Competitor" and c in candidates and c["domain"] not in verified_domains:
            c = {**c, "category": "Organic SERP Competitor", "verification_note": "Downgraded - did not pass LLM similarity check"}
        updated.append(c)
    return updated


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
