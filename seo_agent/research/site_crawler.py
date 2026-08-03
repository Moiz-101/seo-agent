"""Free full-site crawler (a lightweight, self-hosted stand-in for Screaming
Frog, which is a paid desktop tool with no free server-side API). BFS over
same-domain links, respects robots.txt, capped at max_pages so a single
audit stays within Render's free-tier time/resource budget.
"""
import difflib
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
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
            page = parse_page(resp.text, url) if resp.ok else {"url": url}
            page["status_code"] = resp.status_code
            page["final_url"] = resp.url
            page["was_redirected"] = bool(resp.history)
            page["redirect_chain"] = [r.url for r in resp.history] if resp.history else []
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


def find_protocol_issues(pages: list[dict]) -> dict:
    """Distinguishes a proper http-> https redirect (fine, just recorded for
    the redirects export) from a true duplicate-content protocol issue
    (both the http:// and https:// version serve 200 independently, with no
    redirect between them) - this used to just show up as a confusing
    "duplicate title" finding, which understated the real problem.
    """
    by_path = defaultdict(dict)
    for p in pages:
        if p.get("status_code") is None:
            continue
        parsed = urlparse(p["url"])
        by_path[(parsed.netloc.lower(), parsed.path.rstrip("/"))][parsed.scheme] = p

    duplicates = []
    for (_netloc, path), scheme_map in by_path.items():
        http_page, https_page = scheme_map.get("http"), scheme_map.get("https")
        if not http_page or not https_page:
            continue
        if http_page.get("status_code") == 200 and not http_page.get("was_redirected"):
            duplicates.append(
                {
                    "path": path or "/",
                    "http_url": http_page["url"],
                    "https_url": https_page["url"],
                    "http_status": http_page.get("status_code"),
                    "http_canonical": http_page.get("canonical"),
                    "http_indexable": http_page.get("indexable", True),
                    "recommendation": "Set up a 301 redirect from the http:// version to https://, or add a canonical tag on the http:// page pointing to the https:// version.",
                }
            )
    return {"protocol_duplicates": duplicates}


def compute_page_issues(page: dict, broken_source_pages: set, protocol_dup_urls: set) -> list[str]:
    """Every detectable issue for a single page - used by both the XLSX
    page-audit export and the PDF appendix so the two never disagree.
    """
    issues = []
    if not page.get("title"):
        issues.append("missing title")
    elif page.get("title_length", 0) > 60:
        issues.append("title too long")
    if not page.get("meta_description"):
        issues.append("missing meta description")
    if page.get("h1_count") == 0:
        issues.append("missing H1")
    elif (page.get("h1_count") or 1) > 1:
        issues.append("multiple H1")
    if not page.get("canonical"):
        issues.append("missing canonical")
    if not page.get("indexable", True):
        issues.append("noindex")
    if page.get("is_orphan"):
        issues.append("orphan page")
    if page.get("images_missing_alt", 0) > 0:
        issues.append(f"{page['images_missing_alt']} image(s) missing alt")
    if page.get("images_missing_dimensions", 0) > 0:
        issues.append(f"{page['images_missing_dimensions']} image(s) missing width/height")
    if page["url"] in broken_source_pages:
        issues.append("has broken outgoing link(s)")
    if page["url"] in protocol_dup_urls:
        issues.append("http/https duplicate")
    if not page.get("schema_present"):
        issues.append("no structured data")
    return issues


def build_redirects_report(pages: list[dict]) -> list[dict]:
    return [
        {
            "original_url": p["url"],
            "final_url": p.get("final_url", p["url"]),
            "status_code": p.get("status_code"),
            "hop_count": len(p.get("redirect_chain", [])),
        }
        for p in pages
        if p.get("was_redirected")
    ]


def aggregate_schema_report(pages: list[dict]) -> dict:
    """Sitewide structured-data picture: which schema.org @types are
    actually in use, how many pages have invalid (unparseable) JSON-LD, and
    a couple of commonly-missing recommendations. This is a lightweight
    JSON-validity check, not a full schema.org spec validator - there's no
    free tool for that, so we're explicit about the limit.
    """
    type_counts: dict[str, int] = defaultdict(int)
    pages_with_errors = []
    pages_with_schema = 0
    ok_pages = [p for p in pages if p.get("status_code") == 200]

    for p in ok_pages:
        blocks = p.get("schema_blocks", [])
        if not blocks:
            continue
        pages_with_schema += 1
        for b in blocks:
            if b["valid"]:
                type_counts[b["type"]] += 1
            else:
                pages_with_errors.append({"url": p["url"], "error": b["error"]})

    recommendations = []
    has_business_schema = any(t in type_counts for t in ("LocalBusiness", "Organization", "AutoRepair", "Service"))
    if not has_business_schema:
        recommendations.append("No LocalBusiness/Organization schema found sitewide - add this to the homepage so search engines understand the business identity.")
    if "BreadcrumbList" not in type_counts and len(ok_pages) > 5:
        recommendations.append("No BreadcrumbList schema found - useful for multi-level sites to improve how URLs display in search results.")

    return {
        "pages_with_schema": pages_with_schema,
        "pages_total": len(ok_pages),
        "schema_types_found": dict(type_counts),
        "pages_with_invalid_schema": pages_with_errors,
        "recommendations": recommendations,
    }


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
    Returns only groups with 2+ pages (i.e. actual duplicates). Pages that
    redirect elsewhere are excluded - their title/meta reflect the
    redirect *destination*, not a second independent page with duplicate
    content (that's a protocol/canonicalization issue, see
    find_protocol_issues(), not a duplicate-title issue).
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        if p.get("status_code") != 200 or p.get("was_redirected"):
            continue
        value = p.get(field)
        if value:
            groups[value].append(p["url"])

    return [{"value": v, "urls": urls} for v, urls in groups.items() if len(urls) > 1]


def _check_link_status(url: str) -> dict:
    """Returns {'status': int|None, 'failure_reason': str|None, 'confirmed': bool}.
    'confirmed' distinguishes a real HTTP error (definitely broken) from an
    ambiguous result (timeout, DNS/SSL failure, or a 403/429 that's more
    likely our automated check being blocked than a genuinely broken link).
    """
    try:
        resp = requests.head(url, timeout=8, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
        if resp.status_code == 405:  # some servers reject HEAD
            resp = requests.get(url, timeout=8, headers={"User-Agent": USER_AGENT}, stream=True)
        if resp.status_code in (403, 429):
            return {"status": resp.status_code, "failure_reason": "bot_blocked", "confirmed": False}
        return {"status": resp.status_code, "failure_reason": None, "confirmed": True}
    except requests.exceptions.SSLError:
        return {"status": None, "failure_reason": "ssl_failure", "confirmed": False}
    except requests.exceptions.Timeout:
        return {"status": None, "failure_reason": "timeout", "confirmed": False}
    except requests.exceptions.ConnectionError as e:
        msg = str(e).lower()
        if "name or service not known" in msg or "getaddrinfo failed" in msg or "nodename nor servname" in msg:
            reason = "dns_failure"
        elif "connection refused" in msg:
            reason = "connection_refused"
        else:
            reason = "connection_error"
        return {"status": None, "failure_reason": reason, "confirmed": False}
    except Exception:
        return {"status": None, "failure_reason": "unknown_error", "confirmed": False}


def _suggest_redirect_target(broken_url: str, valid_urls: list[str]) -> str | None:
    """Fuzzy-matches a broken internal URL's path against known-valid
    crawled page paths (e.g. '/small-bussiness-seo-services/' looks like a
    typo of '/small-business-seo-services/') using stdlib difflib - no paid
    typo-detection API needed for this.
    """
    broken_path = urlparse(broken_url).path.rstrip("/")
    if not broken_path:
        return None
    valid_paths = {urlparse(u).path.rstrip("/"): u for u in valid_urls if urlparse(u).path.rstrip("/")}
    matches = difflib.get_close_matches(broken_path, valid_paths.keys(), n=1, cutoff=0.75)
    return valid_paths[matches[0]] if matches else None


def build_broken_links_report(pages: list[dict], base_url: str, max_extra_checks: int = 50) -> dict:
    """Separates confirmed-broken links (a real 4xx/5xx we received) from
    unverified ones (timeout/DNS/SSL failure/likely bot-blocking) - and
    reports reconciled counts (unique URLs vs total occurrences vs affected
    source pages) so the summary never contradicts the detailed table.
    """
    netloc = urlparse(base_url).netloc
    status_by_url = {_normalize(p["url"]): p.get("status_code") for p in pages}
    valid_urls = [p["url"] for p in pages if p.get("status_code") == 200]
    checked: dict[str, dict] = {}
    confirmed, unverified = [], []
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
            anchor_text = link["text"] or "(no visible text)"

            known_status = status_by_url.get(absolute)
            if known_status is not None:
                if known_status < 400:
                    continue
                entry = {
                    "source_page": page["url"],
                    "destination": absolute_raw,
                    "status": known_status,
                    "type": "external" if is_external else "internal",
                    "anchor_text": anchor_text,
                    "failure_reason": None,
                }
                if not is_external:
                    entry["suggested_redirect"] = _suggest_redirect_target(absolute_raw, valid_urls)
                confirmed.append(entry)
                continue

            if absolute in checked:
                result = checked[absolute]
            elif extra_checks < max_extra_checks:
                result = _check_link_status(absolute_raw)
                checked[absolute] = result
                extra_checks += 1
            else:
                continue

            if result["status"] is not None and result["status"] < 400:
                continue

            entry = {
                "source_page": page["url"],
                "destination": absolute_raw,
                "status": result["status"] if result["status"] is not None else "no response",
                "type": "external" if is_external else "internal",
                "anchor_text": anchor_text,
                "failure_reason": result["failure_reason"],
            }
            if not is_external:
                entry["suggested_redirect"] = _suggest_redirect_target(absolute_raw, valid_urls)

            (confirmed if result["confirmed"] else unverified).append(entry)

    all_entries = confirmed + unverified
    internal = [e for e in all_entries if e["type"] == "internal"]
    external = [e for e in all_entries if e["type"] == "external"]

    return {
        "confirmed_broken": confirmed,
        "unverified": unverified,
        "unique_internal_broken_urls": len({e["destination"] for e in internal}),
        "internal_broken_occurrences": len(internal),
        "unique_external_unreachable_urls": len({e["destination"] for e in external}),
        "external_unreachable_occurrences": len(external),
        "affected_source_pages": len({e["source_page"] for e in all_entries}),
        "extra_checks_capped": extra_checks >= max_extra_checks,
    }


def build_image_alt_report(pages: list[dict], max_unique_images: int = 60) -> list[dict]:
    """Deduplicates by image asset (a shared logo/icon reused on every page
    would otherwise show up as one finding per page). Images reused across
    3+ pages are treated as decorative/structural (logos, icons) even if
    their filename doesn't hint at it; images unique to 1-2 pages are
    treated as real content images.
    """
    decorative_hints = ("icon", "spacer", "pixel", "divider", "bullet", "arrow", "logo")
    by_image: dict[str, dict] = {}

    for page in pages:
        if page.get("status_code") != 200:
            continue
        for img in page.get("images_detailed", []):
            if img["alt"]:
                continue
            src = img["src"] or ""
            if src.startswith("data:"):
                src = "(inline/lazy-loaded placeholder - actual image source not captured)"
            src = src[:200]

            entry = by_image.setdefault(src, {"image_url": src, "pages": [], "titles": []})
            entry["pages"].append(page["url"])
            if page.get("title"):
                entry["titles"].append(page["title"])

    findings = []
    for src, data in by_image.items():
        pages_using = data["pages"]
        is_decorative = any(hint in src.lower() for hint in decorative_hints) or len(pages_using) >= 3
        findings.append(
            {
                "image_url": src,
                "occurrence_count": len(pages_using),
                "pages": pages_using,
                "page_url": pages_using[0],
                "page_title": data["titles"][0] if data["titles"] else "",
                "sitewide_reused": len(pages_using) >= 3,
                "classification": "Decorative" if is_decorative else "Meaningful",
                "priority": "Low" if is_decorative else "Medium",
                "recommended_alt": 'Likely decorative - alt="" is acceptable' if is_decorative else None,
            }
        )
        if len(findings) >= max_unique_images:
            break

    findings.sort(key=lambda f: (f["classification"] != "Meaningful", -f["occurrence_count"]))
    return findings


def generate_alt_text_suggestions(findings: list[dict], max_generate: int = 20) -> list[dict]:
    """For 'Meaningful' image findings, asks Gemini to draft a specific
    suggested alt text using the page title and image filename/URL as
    context clues. The model never sees the actual image pixels, so this is
    a drafting aid for a human to verify against the real image, not a
    guaranteed-accurate description - findings are labelled accordingly.
    Capped since each suggestion costs one Gemini call.
    """
    import google.generativeai as genai

    from config import GEMINI_API_KEY

    if not GEMINI_API_KEY:
        for f in findings:
            if f["classification"] == "Meaningful" and not f.get("recommended_alt"):
                f["recommended_alt"] = "Add descriptive alt text based on the image's content and this page's topic"
        return findings

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-flash-latest")
    generated = 0

    for f in findings:
        if f["classification"] != "Meaningful":
            continue
        if generated >= max_generate:
            f["recommended_alt"] = "Add descriptive alt text based on the image's content and this page's topic"
            continue
        try:
            prompt = (
                f"A webpage titled \"{f.get('page_title') or f.get('page_url', '')}\" has an image with "
                f"filename/URL \"{f['image_url']}\". Draft a concise, specific HTML alt text (under 125 "
                "characters) for it, inferred from the page topic and filename. If the filename gives no "
                "useful clue, write a generic-but-relevant description tied to the page topic instead of "
                "inventing specifics. Return only the alt text - no quotes, no explanation."
            )
            response = model.generate_content(prompt)
            f["recommended_alt"] = response.text.strip().strip('"')
            f["recommended_alt_ai_generated"] = True
            generated += 1
        except Exception as e:
            logger.warning("Alt-text generation failed for %s: %s", f["image_url"], e)
            f["recommended_alt"] = "Add descriptive alt text based on the image's content and this page's topic"

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
        "pages_crawled_with_error_status": len(broken),  # unique broken URLs the crawler visited directly - see broken_links_report for the full link-level picture
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
