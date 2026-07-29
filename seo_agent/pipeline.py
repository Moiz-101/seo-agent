"""Per-website SEO pipeline state machine.

Stages:
  AUDITING              -> crawling the site + running technical/on-page/speed/
                          authority checks and building the audit PDF
  AUDIT_READY            -> PDF sent on Telegram. Nothing else happens until the
                          user says "Scratch Start" - the agent never invents new
                          pages/topics on its own.
  [Phase B, not yet wired] existing-page content polish/rewrite loop:
  CONTENT_READY         -> a content doc has been generated and sent to Telegram,
                          waiting for the user to hand it to the developer
  AWAITING_DEV_UPDATE   -> user confirmed doc was sent; waiting for "go ahead"
                          (i.e. developer has published it on the live site)
  TECHNICAL_SEO         -> after go-ahead, run on-page/technical audit on the
                          now-updated page and produce a fix-it report
  MONITORING            -> content queue exhausted; periodically checks GSC
                          rankings until the user stops the site
  STOPPED               -> user said stop
"""
from urllib.parse import urlparse

from seo_agent.storage import state_store
from seo_agent.research import site_audit
from seo_agent.research import site_crawler
from seo_agent.research import authority as authority_research
from seo_agent.research import screenshots as screenshot_capture
from seo_agent.research import keywords as kw_research
from seo_agent.research import competitors as competitor_research
from seo_agent.content import generator, docx_writer
from seo_agent.reporting import pdf_report


def start_new_site(url: str) -> dict:
    state_store.create_site(url)
    return run_audit(url)


def _derive_topic(homepage_title: str | None, domain: str) -> str:
    """DuckDuckGo autocomplete returns nothing for long, brand-heavy page
    titles (e.g. 'Car Repair & Maintenance in Dubai | Best Garage Dubai').
    Split on common title separators and keep the first, most topic-like
    segment, trimmed to a handful of words.
    """
    if not homepage_title:
        return domain

    import re

    segment = re.split(r"[|\-–—:]", homepage_title)[0].strip()
    words = segment.split()
    return " ".join(words[:6]) if words else domain


def run_audit(url: str, max_pages: int = 200) -> dict:
    """Crawls the whole site and builds the agency-style audit PDF. Does not
    generate or plan any content - that only happens after "Scratch Start".
    """
    state_store.update_site(url, stage="AUDITING")

    crawl = site_crawler.crawl_site(url, max_pages=max_pages)
    domain = urlparse(url).netloc
    authority_data = authority_research.get_authority_score(domain)

    other_pages = [p for p in crawl["pages"][1:] if p.get("status_code") == 200 and p["url"] != url]

    sample_urls = [url] + [p["url"] for p in other_pages[:5]]
    performance_samples = []
    for sample_url in sample_urls:
        try:
            perf = site_audit.audit_performance(sample_url)
            perf["url"] = sample_url
            performance_samples.append(perf)
        except Exception:
            continue

    screenshot_targets = [("Homepage", url)] + [
        (p.get("title") or p["url"], p["url"]) for p in other_pages[:2]
    ]
    screenshots_list = []
    for label, shot_url in screenshot_targets:
        img = screenshot_capture.capture_screenshot(shot_url)
        if img and len(img) > 2000:  # guards against a broken/placeholder image slipping through
            screenshots_list.append((label, img))

    homepage = crawl["pages"][0] if crawl["pages"] else {}
    topic = _derive_topic(homepage.get("title"), domain)
    keyword_ideas = kw_research.autocomplete_only(topic)
    competitors = competitor_research.find_competitors(topic, domain, max_results=8)

    site_data = {
        "url": url,
        "crawl": crawl,
        "authority": authority_data,
        "performance_samples": performance_samples,
        "screenshots": screenshots_list,
        "keyword_ideas": keyword_ideas,
        "competitors": competitors,
    }
    pdf_path = pdf_report.build_audit_pdf(site_data)

    site = state_store.update_site(
        url,
        stage="AUDIT_READY",
        crawl_stats=crawl["stats"],
        crawl_page_count=len(crawl["pages"]),
    )
    return {"site": site, "pdf_path": pdf_path}


def generate_next_content_doc(url: str) -> tuple[dict, str] | None:
    """Pops the next content brief off the queue, generates the article,
    saves it as a .docx, returns (site, docx_path). Returns None if the
    queue is empty (site should move to MONITORING).
    """
    site = state_store.get_site(url)
    queue = site["content_queue"]
    if not queue:
        return None

    brief = queue[0]
    article_md = generator.generate_article(
        domain=brief["domain"],
        primary_keyword=brief["primary_keyword"],
        secondary_keywords=brief["secondary_keywords"],
    )
    docx_path = docx_writer.save_article_as_docx(article_md, brief["primary_keyword"])

    remaining_queue = queue[1:]
    published = site["published_topics"] + [brief["primary_keyword"]]
    site = state_store.update_site(
        url, stage="CONTENT_READY", content_queue=remaining_queue, published_topics=published
    )
    return site, docx_path


def mark_sent_to_dev(url: str) -> dict:
    return state_store.update_site(url, stage="AWAITING_DEV_UPDATE")


def handle_go_ahead(url: str) -> dict:
    """Called when the user tells the bot the developer has published the
    update. Runs a technical/on-page audit of the live site and produces a
    report doc, then either queues the next content piece or moves to
    monitoring if the queue is empty.
    """
    audit = site_audit.full_audit(url)
    report_md = _build_audit_report_md(url, audit)
    report_path = docx_writer.markdown_to_docx(report_md, "technical-seo-report.docx")

    site = state_store.get_site(url)
    if site["content_queue"]:
        state_store.update_site(url, stage="TECHNICAL_SEO_DONE")
    else:
        state_store.update_site(url, stage="MONITORING")

    return {"site": state_store.get_site(url), "report_path": report_path, "audit": audit}


def _build_audit_report_md(url: str, audit: dict) -> str:
    on_page = audit.get("on_page") or {}
    perf = audit.get("performance") or {}

    lines = [f"# Technical SEO Report - {url}", ""]
    lines.append("## On-Page Findings")
    lines.append(f"- Title: {on_page.get('title')} ({on_page.get('title_length')} chars)")
    lines.append(f"- Meta description: {on_page.get('meta_description')} ({on_page.get('meta_description_length')} chars)")
    lines.append(f"- H1 count: {on_page.get('h1_count')}")
    lines.append(f"- Word count: {on_page.get('word_count')}")
    lines.append(f"- Images missing alt text: {on_page.get('images_missing_alt')} / {on_page.get('images_total')}")
    lines.append("")
    lines.append("## Performance (PageSpeed Insights)")
    lines.append(f"- Performance score: {perf.get('performance_score')}")
    lines.append(f"- SEO score: {perf.get('seo_score')}")
    lines.append(f"- Largest Contentful Paint: {perf.get('largest_contentful_paint')}")
    lines.append(f"- Cumulative Layout Shift: {perf.get('cumulative_layout_shift')}")
    lines.append("")
    lines.append("## Recommendations")
    if on_page.get("title_length", 0) > 60:
        lines.append("- Shorten the page title to under 60 characters.")
    if not on_page.get("meta_description"):
        lines.append("- Add a meta description (under 155 characters).")
    if on_page.get("h1_count") != 1:
        lines.append("- Page should have exactly one H1 tag.")
    if on_page.get("images_missing_alt", 0) > 0:
        lines.append("- Add descriptive alt text to all images.")

    return "\n".join(lines)
