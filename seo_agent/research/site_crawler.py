"""Free full-site crawler (a lightweight, self-hosted stand-in for Screaming
Frog, which is a paid desktop tool with no free server-side API). BFS over
same-domain links, respects robots.txt, capped at max_pages so a single
audit stays within Render's free-tier time/resource budget.
"""
import logging
import re
import time
from collections import defaultdict, deque
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from seo_agent.research.site_audit import parse_page

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (SEO-Agent Crawler)"
REQUEST_TIMEOUT = 10

# CMS/utility paths that aren't real SEO-relevant content - crawling and
# scoring these skews an audit (e.g. a WordPress site's /wp-admin/ or
# search-results pages showing up as "pages missing a title").
SYSTEM_URL_PATTERNS = [
    r"/wp-admin/",
    r"/wp-login\.php",
    r"action=lostpassword",
    r"redirect_to=",
    r"/feed/?($|\?)",
    r"[?&]feed=",
    r"preview=true",
    r"/search/?($|\?)",
    r"[?&]s=",
    r"/wp-json/",
    r"/xmlrpc\.php",
    r"/cart/?($|\?)",
    r"/checkout/?($|\?)",
    r"/my-account/?($|\?)",
]

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
}


def _is_system_url(url: str) -> bool:
    lower = url.lower()
    return any(re.search(pattern, lower) for pattern in SYSTEM_URL_PATTERNS)


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
    """Strips the fragment and common tracking query params so
    '/page?utm_source=x' and '/page' are treated as the same URL.
    """
    parsed = urlparse(url)
    clean_query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in TRACKING_PARAMS])
    return parsed._replace(fragment="", query=clean_query).geturl().rstrip("/")


def crawl_site(base_url: str, max_pages: int = 200) -> dict:
    """Returns {'pages': [...], 'stats': {...}}. Best-effort per page: a
    failed fetch is recorded with its error rather than aborting the crawl.
    """
    netloc = urlparse(base_url).netloc
    base_scheme = urlparse(base_url).scheme
    robot_parser = _get_robot_parser(base_url)

    start = _normalize(base_url)
    seen = {start}
    queue = deque([(base_url, 0)])
    pages = []
    incoming_links = defaultdict(int)
    mixed_protocol_links = 0

    while queue and len(pages) < max_pages:
        url, depth = queue.popleft()

        if _is_system_url(url):
            continue

        allow_all = hasattr(robot_parser, "allow_all") and robot_parser.allow_all
        if not allow_all and not robot_parser.can_fetch(USER_AGENT, url):
            continue

        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
            page = parse_page(resp.text, url) if resp.ok else {"url": url}
            page["status_code"] = resp.status_code
        except Exception as e:
            logger.warning("Crawl failed for %s: %s", url, e)
            pages.append({"url": url, "status_code": None, "error": str(e), "depth": depth})
            continue

        page["depth"] = depth
        pages.append(page)

        for link in page.get("links", []):
            absolute_raw = urljoin(url, link)
            absolute = _normalize(absolute_raw)
            if _is_system_url(absolute) or not absolute.startswith(("http://", "https://")):
                continue
            if not _is_same_domain(absolute, netloc):
                continue

            if urlparse(absolute_raw).scheme != base_scheme:
                mixed_protocol_links += 1

            incoming_links[absolute] += 1
            if absolute not in seen:
                seen.add(absolute)
                queue.append((absolute, depth + 1))

        time.sleep(0.3)  # be polite, avoid hammering the target site

    for page in pages:
        normalized = _normalize(page["url"])
        page["incoming_internal_links"] = incoming_links.get(normalized, 0)
        page["is_orphan"] = normalized != start and page["incoming_internal_links"] == 0

        outgoing_internal = 0
        for link in page.get("links_detailed", []):
            abs_link = _normalize(urljoin(page["url"], link["href"]))
            if _is_same_domain(abs_link, netloc):
                outgoing_internal += 1
        page["internal_link_count"] = outgoing_internal

    stats = _aggregate_stats(pages, max_pages, len(queue) > 0)
    stats["mixed_protocol_links"] = mixed_protocol_links
    return {"pages": pages, "stats": stats}


def check_robots_and_sitemap(base_url: str) -> dict:
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    result = {}

    try:
        r = requests.get(f"{origin}/robots.txt", timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        result["robots_txt_present"] = r.status_code == 200
        result["sitemap_declared_in_robots"] = r.status_code == 200 and "sitemap:" in r.text.lower()
    except Exception as e:
        result["robots_txt_present"] = False
        result["robots_txt_error"] = str(e)

    try:
        r = requests.get(f"{origin}/sitemap.xml", timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        result["sitemap_xml_present"] = r.status_code == 200
        result["sitemap_url_count"] = r.text.count("<loc>") if r.status_code == 200 else 0
    except Exception as e:
        result["sitemap_xml_present"] = False
        result["sitemap_xml_error"] = str(e)

    return result


def find_duplicate_groups(pages: list[dict], field: str) -> list[dict]:
    """Groups crawled pages that share the exact same title/meta description.
    Returns only groups with 2+ pages (i.e. actual duplicates)."""
    groups: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        if p.get("status_code") != 200:
            continue
        value = p.get(field)
        if value:
            groups[value].append(p["url"])

    return [{"value": v, "urls": urls} for v, urls in groups.items() if len(urls) > 1]


def _check_link_status(url: str) -> int | None:
    try:
        resp = requests.head(url, timeout=8, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
        if resp.status_code == 405:  # some servers reject HEAD
            resp = requests.get(url, timeout=8, headers={"User-Agent": USER_AGENT}, stream=True)
        return resp.status_code
    except Exception:
        return None


def build_broken_links_report(pages: list[dict], base_url: str, max_extra_checks: int = 50) -> dict:
    """Finds broken links (4xx/5xx or unreachable) across the crawled pages.
    Internal links already covered by the crawl are free (status known); we
    only spend extra live HTTP requests on internal links the crawl didn't
    reach (skipped by the page cap/robots.txt) and on external links -
    capped so a link-heavy site doesn't blow out audit time.
    """
    netloc = urlparse(base_url).netloc
    status_by_url = {_normalize(p["url"]): p.get("status_code") for p in pages}
    checked = {}
    broken = []
    extra_checks = 0

    for page in pages:
        if page.get("status_code") != 200:
            continue
        for link in page.get("links_detailed", []):
            href = link["href"]
            if href.startswith(("mailto:", "tel:", "javascript:", "#")) or not href:
                continue

            absolute_raw = urljoin(page["url"], href)
            absolute = _normalize(absolute_raw)
            is_external = not _is_same_domain(absolute, netloc)

            status = status_by_url.get(absolute)
            if status is None:
                if absolute in checked:
                    status = checked[absolute]
                elif extra_checks < max_extra_checks:
                    status = _check_link_status(absolute_raw)
                    checked[absolute] = status
                    extra_checks += 1
                else:
                    continue  # over budget, skip rather than slow the audit down further

            if status is None or status >= 400:
                broken.append(
                    {
                        "source_page": page["url"],
                        "destination": absolute_raw,
                        "status": status if status is not None else "unreachable",
                        "type": "external" if is_external else "internal",
                        "anchor_text": link["text"] or "(no visible text)",
                        "recommended_fix": (
                            "Update or remove this external link"
                            if is_external
                            else "Fix the internal link or set up a 301 redirect for the destination"
                        ),
                    }
                )

    return {"broken_links": broken, "extra_checks_capped": extra_checks >= max_extra_checks}


def build_image_alt_report(pages: list[dict], max_entries: int = 60) -> list[dict]:
    """Every image missing alt text, with a decorative/meaningful guess and
    priority - capped so a very image-heavy site doesn't blow out the PDF.
    """
    findings = []
    decorative_hints = ("icon", "spacer", "pixel", "divider", "bullet", "arrow", "logo-small")

    for page in pages:
        if page.get("status_code") != 200:
            continue
        for img in page.get("images_detailed", []):
            if img["alt"]:
                continue
            src = img["src"] or ""
            if src.startswith("data:"):
                src = "(inline/lazy-loaded placeholder - actual image source not captured)"
            is_decorative = any(hint in src.lower() for hint in decorative_hints)
            findings.append(
                {
                    "page_url": page["url"],
                    "image_url": src[:120],
                    "classification": "Decorative" if is_decorative else "Meaningful",
                    "recommended_alt": (
                        'Likely decorative - alt="" is acceptable'
                        if is_decorative
                        else "Add descriptive alt text based on the image's content and this page's topic"
                    ),
                    "priority": "Low" if is_decorative else "Medium",
                }
            )
            if len(findings) >= max_entries:
                return findings
    return findings


def _aggregate_stats(pages: list[dict], max_pages: int, capped: bool) -> dict:
    total = len(pages)
    ok_pages = [p for p in pages if p.get("status_code") == 200]

    broken = [p for p in pages if p.get("status_code") and p["status_code"] >= 400]
    missing_title = [p for p in ok_pages if not p.get("title")]
    missing_meta = [p for p in ok_pages if not p.get("meta_description")]
    missing_h1 = [p for p in ok_pages if p.get("h1_count") == 0]
    multi_h1 = [p for p in ok_pages if (p.get("h1_count") or 0) > 1]
    non_indexable = [p for p in ok_pages if not p.get("indexable", True)]
    orphans = [p for p in ok_pages if p.get("is_orphan")]
    missing_schema = [p for p in ok_pages if not p.get("schema_present")]
    images_missing_alt_total = sum(p.get("images_missing_alt", 0) for p in ok_pages)
    images_missing_dimensions_total = sum(p.get("images_missing_dimensions", 0) for p in ok_pages)
    word_counts = [p["word_count"] for p in ok_pages if p.get("word_count") is not None]
    depths = [p.get("depth", 0) for p in ok_pages]

    return {
        "total_pages_crawled": total,
        "crawl_capped": capped and total >= max_pages,
        "broken_links": len(broken),
        "pages_missing_title": len(missing_title),
        "pages_missing_meta": len(missing_meta),
        "pages_missing_h1": len(missing_h1),
        "pages_multiple_h1": len(multi_h1),
        "non_indexable_pages": len(non_indexable),
        "orphan_pages": len(orphans),
        "pages_missing_schema": len(missing_schema),
        "images_missing_alt_total": images_missing_alt_total,
        "images_missing_dimensions_total": images_missing_dimensions_total,
        "avg_word_count": round(sum(word_counts) / len(word_counts)) if word_counts else 0,
        "max_crawl_depth": max(depths) if depths else 0,
    }
