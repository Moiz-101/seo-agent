"""Basic on-page + technical audit using free sources:
- direct HTML fetch + BeautifulSoup for on-page elements
- Google PageSpeed Insights API (free, needs a Google Cloud API key)
"""
import json

import requests
from bs4 import BeautifulSoup

from config import PAGESPEED_API_KEY

PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def parse_page(html: str, url: str) -> dict:
    """Shared on-page parsing used by both the single-page audit and the
    site-wide crawler (seo_agent/research/site_crawler.py).
    """
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.string.strip() if soup.title and soup.title.string else None
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_desc_tag["content"].strip() if meta_desc_tag and meta_desc_tag.get("content") else None

    canonical_tag = soup.find("link", attrs={"rel": "canonical"})
    canonical = canonical_tag["href"].strip() if canonical_tag and canonical_tag.get("href") else None

    robots_tag = soup.find("meta", attrs={"name": "robots"})
    robots_content = (robots_tag.get("content") or "").lower() if robots_tag else ""
    indexable = "noindex" not in robots_content

    h1s = [h.get_text(strip=True) for h in soup.find_all("h1")]
    h2s = [h.get_text(strip=True) for h in soup.find_all("h2")]

    images = soup.find_all("img")
    LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-lazy")

    def _real_src(img):
        src = img.get("src", "")
        if src.startswith("data:"):
            for attr in LAZY_SRC_ATTRS:
                lazy_src = img.get(attr)
                if lazy_src:
                    return lazy_src
        return src

    images_detailed = [
        {
            "src": _real_src(img),
            "alt": img.get("alt"),
            "has_dimensions": bool(img.get("width") and img.get("height")),
        }
        for img in images
    ]
    images_missing_alt_list = [i for i in images_detailed if not i["alt"]]

    links = soup.find_all("a", href=True)
    links_detailed = [{"href": a["href"], "text": a.get_text(strip=True)} for a in links]

    schema_blocks = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    schema_blocks.append({"type": item.get("@type", "Unknown"), "valid": True, "error": None})
        except Exception as e:
            schema_blocks.append({"type": None, "valid": False, "error": str(e)[:150]})

    schema_present = len(schema_blocks) > 0
    schema_types = [b["type"] for b in schema_blocks if b["valid"] and b["type"]]
    schema_has_errors = any(not b["valid"] for b in schema_blocks)
    word_count = len(soup.get_text(separator=" ", strip=True).split())

    return {
        "url": url,
        "title": title,
        "title_length": len(title) if title else 0,
        "meta_description": meta_description,
        "meta_description_length": len(meta_description) if meta_description else 0,
        "canonical": canonical,
        "indexable": indexable,
        "h1_count": len(h1s),
        "h1_tags": h1s,
        "h2_tags": h2s,
        "word_count": word_count,
        "images_total": len(images),
        "images_missing_alt": len(images_missing_alt_list),
        "images_missing_alt_list": images_missing_alt_list,
        "images_detailed": images_detailed,
        "images_missing_dimensions": sum(1 for i in images_detailed if not i["has_dimensions"]),
        "schema_present": schema_present,
        "schema_types": schema_types,
        "schema_has_errors": schema_has_errors,
        "schema_blocks": schema_blocks,
        "links": [l["href"] for l in links_detailed],
        "links_detailed": links_detailed,
        "internal_link_count": None,  # filled in by the crawler, which knows the site's domain
    }


def audit_on_page(url: str) -> dict:
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (SEO-Agent)"})
    resp.raise_for_status()
    return parse_page(resp.text, url)


def audit_performance(url: str, strategy: str = "mobile") -> dict:
    if not PAGESPEED_API_KEY:
        return {"error": "PAGESPEED_API_KEY not set, skipping performance audit"}

    params = {
        "url": url,
        "strategy": strategy,
        "key": PAGESPEED_API_KEY,
        "category": ["PERFORMANCE", "SEO", "ACCESSIBILITY"],
    }
    resp = requests.get(PAGESPEED_ENDPOINT, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    lighthouse = data.get("lighthouseResult", {})
    categories = lighthouse.get("categories", {})
    audits = lighthouse.get("audits", {})

    return {
        "performance_score": categories.get("performance", {}).get("score"),
        "seo_score": categories.get("seo", {}).get("score"),
        "accessibility_score": categories.get("accessibility", {}).get("score"),
        "largest_contentful_paint": audits.get("largest-contentful-paint", {}).get("displayValue"),
        "cumulative_layout_shift": audits.get("cumulative-layout-shift", {}).get("displayValue"),
        "total_blocking_time": audits.get("total-blocking-time", {}).get("displayValue"),
    }


def full_audit(url: str) -> dict:
    result = {"on_page": None, "performance": None, "error": None}
    try:
        result["on_page"] = audit_on_page(url)
    except Exception as e:
        result["error"] = f"on_page audit failed: {e}"

    try:
        result["performance"] = audit_performance(url)
    except Exception as e:
        result["performance"] = {"error": str(e)}

    return result
