"""Free full-site crawler (a lightweight, self-hosted stand-in for Screaming
Frog, which is a paid desktop tool with no free server-side API). BFS over
same-domain links, respects robots.txt, capped at max_pages so a single
audit stays within Render's free-tier time/resource budget.
"""
import logging
import time
from collections import deque
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from seo_agent.research.site_audit import parse_page

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (SEO-Agent Crawler)"
REQUEST_TIMEOUT = 10


def _get_robot_parser(base_url: str) -> RobotFileParser:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        resp = requests.get(robots_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        else:
            rp.allow_all = True
    except Exception:
        rp.allow_all = True
    return rp


def _is_same_domain(url: str, netloc: str) -> bool:
    return urlparse(url).netloc == netloc


def _normalize(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl().rstrip("/")


def crawl_site(base_url: str, max_pages: int = 200) -> dict:
    """Returns {'pages': [...], 'stats': {...}}. Best-effort per page: a
    failed fetch is recorded with its error rather than aborting the crawl.
    """
    netloc = urlparse(base_url).netloc
    robot_parser = _get_robot_parser(base_url)

    seen = {_normalize(base_url)}
    queue = deque([base_url])
    pages = []

    while queue and len(pages) < max_pages:
        url = queue.popleft()

        if hasattr(robot_parser, "allow_all") and robot_parser.allow_all:
            allowed = True
        else:
            allowed = robot_parser.can_fetch(USER_AGENT, url)
        if not allowed:
            continue

        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
            page = parse_page(resp.text, url) if resp.ok else {"url": url}
            page["status_code"] = resp.status_code
        except Exception as e:
            logger.warning("Crawl failed for %s: %s", url, e)
            pages.append({"url": url, "status_code": None, "error": str(e)})
            continue

        pages.append(page)

        for link in page.get("links", []):
            absolute = _normalize(urljoin(url, link))
            if absolute not in seen and _is_same_domain(absolute, netloc) and absolute.startswith(("http://", "https://")):
                seen.add(absolute)
                queue.append(absolute)

        time.sleep(0.3)  # be polite, avoid hammering the target site

    return {"pages": pages, "stats": _aggregate_stats(pages, max_pages, len(queue) > 0)}


def _aggregate_stats(pages: list[dict], max_pages: int, capped: bool) -> dict:
    total = len(pages)
    broken = [p for p in pages if p.get("status_code") and p["status_code"] >= 400]
    missing_title = [p for p in pages if p.get("status_code") == 200 and not p.get("title")]
    missing_meta = [p for p in pages if p.get("status_code") == 200 and not p.get("meta_description")]
    missing_h1 = [p for p in pages if p.get("status_code") == 200 and p.get("h1_count") == 0]
    multi_h1 = [p for p in pages if p.get("status_code") == 200 and (p.get("h1_count") or 0) > 1]
    non_indexable = [p for p in pages if p.get("status_code") == 200 and not p.get("indexable", True)]
    images_missing_alt_total = sum(p.get("images_missing_alt", 0) for p in pages if p.get("status_code") == 200)
    word_counts = [p["word_count"] for p in pages if p.get("status_code") == 200 and p.get("word_count") is not None]

    return {
        "total_pages_crawled": total,
        "crawl_capped": capped and total >= max_pages,
        "broken_links": len(broken),
        "pages_missing_title": len(missing_title),
        "pages_missing_meta": len(missing_meta),
        "pages_missing_h1": len(missing_h1),
        "pages_multiple_h1": len(multi_h1),
        "non_indexable_pages": len(non_indexable),
        "images_missing_alt_total": images_missing_alt_total,
        "avg_word_count": round(sum(word_counts) / len(word_counts)) if word_counts else 0,
    }
