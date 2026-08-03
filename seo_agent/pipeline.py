"""Per-website SEO pipeline state machine.

Stages:
  AUDITING              -> crawling the site + running technical/on-page/speed/
                          authority checks and building the audit PDF
  AUDIT_READY            -> PDF sent on Telegram. Nothing else happens until the
                          user says "Scratch Start" - the agent never invents new
                          pages/topics on its own.
  SCRATCH_ACTIVE         -> existing pages queued for POLISH (has ranking signal)
                          or REWRITE (no ranking signal yet); one page at a time
  CONTENT_READY         -> a content doc has been generated and sent to Telegram,
                          waiting for the user to hand it to the developer
  AWAITING_DEV_UPDATE   -> user confirmed doc was sent; waiting for "go ahead"
                          (i.e. developer has published it on the live site)
  TECHNICAL_SEO         -> after go-ahead, run on-page/technical audit on the
                          specific page just updated and produce a fix-it report
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
from seo_agent.reporting import pdf_report, xlsx_export, consistency_check


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


def _derive_brand_name(homepage_title: str | None, domain: str) -> str:
    """Opposite end of _derive_topic(): the brand name is usually the LAST
    segment of a title like 'Car Repair & Maintenance in Dubai | Best
    Garage Dubai' (i.e. "Best Garage Dubai"), used for own-brand keyword
    detection and the competitor-verification prompt.
    """
    if not homepage_title:
        return domain
    import re

    segments = re.split(r"[|\-–—:]", homepage_title)
    return segments[-1].strip() if len(segments) > 1 else homepage_title.strip()


def _derive_page_topic(page_url: str, title: str | None) -> str:
    """Prefer the URL slug over the page's own <title> when deriving a
    keyword-research topic for a single page. The pages this queue works on
    are picked *because* they have weak/thin titles (e.g. "Repair &
    Maintenance - bestgaragedubai.com"), and querying autocomplete with a
    generic fragment like that returns irrelevant results (seen in testing:
    it returned an Indian tax/HSN-code suggestion for a Dubai car garage).
    The URL slug is usually still deliberately keyword-focused even when the
    title tag is weak, e.g. /car-repair-maintenance-service-dubai.
    """
    import re
    from urllib.parse import urlparse

    path = urlparse(page_url).path.strip("/")
    slug = path.split("/")[-1] if path else ""
    slug_words = re.sub(r"[-_]+", " ", slug).strip()

    if len(slug_words.split()) >= 2:
        return " ".join(slug_words.split()[:6])

    return _derive_topic(title, slug_words or urlparse(page_url).netloc)


def run_audit(url: str, max_pages: int = 200) -> dict:
    """Crawls the whole site and builds the agency-style audit PDF. Does not
    generate or plan any content - that only happens after "Scratch Start".
    """
    state_store.update_site(url, stage="AUDITING")

    crawl = site_crawler.crawl_site(url, max_pages=max_pages)
    # crawl_site resolves redirects (e.g. bare domain -> www subdomain) before
    # crawling, so the first crawled page reflects where the site actually
    # lives - use that for the domain rather than the possibly-unresolved
    # input url, so authority/competitor lookups target the real domain.
    resolved_homepage_url = crawl["pages"][0]["url"] if crawl["pages"] else url
    domain = urlparse(resolved_homepage_url).netloc
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
    brand_name = _derive_brand_name(homepage.get("title"), domain)
    business_summary = f"{brand_name} - {homepage.get('title', '')}: {homepage.get('meta_description', '') or ''}".strip()

    keyword_ideas = kw_research.autocomplete_only(topic)

    competitor_seed_keywords = [topic] + [k["keyword"] for k in keyword_ideas[:4]]
    competitors = competitor_research.find_competitors_classified(competitor_seed_keywords, domain)
    competitors = competitor_research.verify_direct_competitors(competitors, business_summary)

    competitor_domains = [c["domain"] for c in competitors]
    keyword_ideas = kw_research.classify_keywords(keyword_ideas, brand_name, competitor_domains, home_location=topic.split()[-1] if topic else None)

    technical_checks = site_crawler.check_robots_and_sitemap(url)
    broken_links_report = site_crawler.build_broken_links_report(crawl["pages"], url)
    image_alt_findings = site_crawler.build_image_alt_report(crawl["pages"])
    image_alt_findings = site_crawler.generate_alt_text_suggestions(image_alt_findings)
    duplicate_titles = site_crawler.find_duplicate_groups(crawl["pages"], "title")
    duplicate_metas = site_crawler.find_duplicate_groups(crawl["pages"], "meta_description")
    protocol_issues = site_crawler.find_protocol_issues(crawl["pages"])
    redirects_report = site_crawler.build_redirects_report(crawl["pages"])
    schema_report = site_crawler.aggregate_schema_report(crawl["pages"])

    site_data = {
        "url": url,
        "crawl": crawl,
        "authority": authority_data,
        "performance_samples": performance_samples,
        "screenshots": screenshots_list,
        "keyword_ideas": keyword_ideas,
        "competitors": competitors,
        "technical_checks": technical_checks,
        "broken_links_report": broken_links_report,
        "image_alt_findings": image_alt_findings,
        "duplicate_titles": duplicate_titles,
        "duplicate_metas": duplicate_metas,
        "protocol_issues": protocol_issues,
        "redirects_report": redirects_report,
        "schema_report": schema_report,
    }
    pdf_path = pdf_report.build_audit_pdf(site_data)
    xlsx_paths = xlsx_export.build_xlsx_package(site_data)
    consistency_issues = consistency_check.run_checks(site_data)

    site = state_store.update_site(
        url,
        stage="AUDIT_READY",
        crawl_stats=crawl["stats"],
        crawl_page_count=len(crawl["pages"]),
        audit_consistency_passed=len(consistency_issues) == 0,
        audit_consistency_issues=consistency_issues,
    )
    return {"site": site, "pdf_path": pdf_path, "xlsx_paths": xlsx_paths, "consistency_issues": consistency_issues}


def start_scratch(url: str, max_pages_to_optimize: int = 15) -> dict:
    """Builds the existing-page content queue after the user says "Scratch
    Start". Only ever works on pages that already exist on the site - never
    invents new pages/topics.

    Every page defaults to REWRITE mode: real ranking data (needed to decide
    a page already performs well enough to just POLISH) requires Search
    Console to be connected for this specific site, which isn't wired up by
    default. Pages with the most on-page issues are queued first since they
    need the most work.
    """
    site = state_store.get_site(url)
    if not site or site["stage"] != "AUDIT_READY":
        current = site["stage"] if site else "not tracked"
        raise ValueError(f"'{url}' must be in AUDIT_READY stage to start scratch (currently: {current})")

    if not site.get("audit_consistency_passed", False):
        issues = site.get("audit_consistency_issues") or ["unknown consistency failure"]
        raise ValueError(
            "The last audit did not pass its consistency checks, so Scratch Start is blocked until it's "
            f"re-run and passes: {'; '.join(issues)}"
        )

    crawl = site_crawler.crawl_site(url, max_pages=200)
    candidates = [p for p in crawl["pages"] if p.get("status_code") == 200 and p.get("indexable", True)]

    def issue_score(p):
        score = 0
        if not p.get("title"):
            score += 3
        if not p.get("meta_description"):
            score += 2
        if p.get("h1_count") != 1:
            score += 2
        return score

    candidates.sort(key=issue_score, reverse=True)
    selected = candidates[:max_pages_to_optimize]

    content_queue = [
        {"url": p["url"], "mode": "REWRITE", "title": p.get("title"), "meta_description": p.get("meta_description")}
        for p in selected
    ]

    return state_store.update_site(
        url, stage="SCRATCH_ACTIVE", content_queue=content_queue, published_topics=[]
    )


def generate_next_content_doc(url: str) -> tuple[dict, str] | None:
    """Pops the next page off the queue, generates its updated content,
    saves it as a .docx, returns (site, docx_path). Returns None if the
    queue is empty (site should move to MONITORING).
    """
    site = state_store.get_site(url)
    queue = site["content_queue"]
    if not queue:
        return None

    item = queue[0]
    page_url = item["url"]

    try:
        current = site_audit.audit_on_page(page_url)
    except Exception:
        current = {}

    topic = _derive_page_topic(page_url, item.get("title") or current.get("title"))
    keyword_ideas = kw_research.autocomplete_only(topic)
    target_keyword = keyword_ideas[0]["keyword"] if keyword_ideas else topic

    article_md = generator.generate_page_update(
        page_url=page_url,
        mode=item["mode"],
        current_title=current.get("title") or item.get("title"),
        current_meta=current.get("meta_description") or item.get("meta_description"),
        target_keyword=target_keyword,
    )
    docx_path = docx_writer.save_article_as_docx(article_md, target_keyword)

    remaining_queue = queue[1:]
    published = site["published_topics"] + [page_url]
    site = state_store.update_site(
        url,
        stage="CONTENT_READY",
        content_queue=remaining_queue,
        published_topics=published,
        current_page_url=page_url,
    )
    return site, docx_path


def mark_sent_to_dev(url: str) -> dict:
    return state_store.update_site(url, stage="AWAITING_DEV_UPDATE")


def handle_go_ahead(url: str) -> dict:
    """Called when the user tells the bot the developer has published the
    update. Runs a technical/on-page audit of the *specific page* that was
    just implemented (not just the site's homepage) and produces a report
    doc, then either queues the next content piece or moves to monitoring
    if the queue is empty.
    """
    site = state_store.get_site(url)
    audit_target = site.get("current_page_url") or url
    audit = site_audit.full_audit(audit_target)
    report_md = _build_audit_report_md(audit_target, audit)
    report_path = docx_writer.markdown_to_docx(report_md, "technical-seo-report.docx")

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
