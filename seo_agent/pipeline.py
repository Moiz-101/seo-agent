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
import os
from datetime import datetime
from urllib.parse import urlparse

import config
from seo_agent.storage import state_store
from seo_agent.research import site_audit
from seo_agent.research import site_crawler
from seo_agent.research import authority as authority_research
from seo_agent.research import screenshots as screenshot_capture
from seo_agent.research import keywords as kw_research
from seo_agent.research import competitors as competitor_research
from seo_agent.content import generator, docx_writer
from seo_agent.reporting import pdf_report, xlsx_export, consistency_check
from seo_agent.tracking import search_console


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


def _compute_page_flaws(page_data: dict, use_fallback_message: bool = True) -> list[str]:
    """Deterministic (not LLM-guessed) flaw list for a single page, fed into
    the content-generation prompt and shown to the owner as a strategy
    brief before the full article - "identify content flaws" +
    "create a page-level content strategy" as visible steps, not just an
    implicit jump straight to generated prose. Also reused by monitoring
    (use_fallback_message=False there) to diff real flaw sets between
    checks without a placeholder string polluting the comparison.
    """
    flaws = []
    if not page_data.get("title"):
        flaws.append("missing title tag")
    elif page_data.get("title_length", 0) > 60:
        flaws.append("title too long (over 60 characters)")
    elif page_data.get("title_length", 0) < 20:
        flaws.append("title too short/thin")
    if not page_data.get("meta_description"):
        flaws.append("missing meta description")
    if page_data.get("h1_count", 0) == 0:
        flaws.append("missing H1 heading")
    elif page_data.get("h1_count", 0) > 1:
        flaws.append("multiple H1 headings")
    if page_data.get("word_count", 0) < 300:
        flaws.append("thin content (under 300 words)")
    if page_data.get("images_missing_alt", 0) > 0:
        flaws.append(f"{page_data['images_missing_alt']} image(s) missing alt text")
    if not page_data.get("schema_present"):
        flaws.append("no structured data")
    if flaws or not use_fallback_message:
        return flaws
    return ["no major on-page flaws detected - focus on strengthening keyword targeting and content depth"]


def _extract_title_meta(article_md: str) -> tuple[str, str]:
    import re

    title_match = re.search(r"##\s*Title\s*\n+(.+)", article_md)
    meta_match = re.search(r"##\s*Meta Description\s*\n+(.+)", article_md)
    return (title_match.group(1).strip() if title_match else ""), (meta_match.group(1).strip() if meta_match else "")


def _build_strategy_brief_md(page_url: str, mode: str, target_keyword: str, flaws: list[str]) -> str:
    lines = [
        "## Content Strategy Brief",
        f"- **Page:** {page_url}",
        f"- **Approach:** {'Polish existing content' if mode == 'POLISH' else 'Rewrite'}",
        f"- **Target keyword:** {target_keyword}",
        "- **Flaws identified on the current page:**",
    ]
    lines += [f"  - {f}" for f in flaws]
    lines += ["", "---", ""]
    return "\n".join(lines)


def _generate_content_for_page(url: str, page_url: str, mode: str, stored_title: str | None, stored_meta: str | None, revision_notes: str | None = None) -> tuple[str, str]:
    """Shared by generate_next_content_doc and regenerate_content_with_revision:
    fetches live on-page data, computes flaws, generates the article (with the
    strategy brief prepended), and returns (docx_path, target_keyword). Also
    stores the proposal (title/meta we're suggesting) in state so a later
    "go ahead" can verify what actually went live against what we proposed.
    """
    try:
        current = site_audit.audit_on_page(page_url)
    except Exception:
        current = {}

    flaws = _compute_page_flaws(current) if current else ["could not re-fetch the live page to check for flaws"]
    topic = _derive_page_topic(page_url, stored_title or current.get("title"))
    keyword_ideas = kw_research.autocomplete_only(topic)
    target_keyword = keyword_ideas[0]["keyword"] if keyword_ideas else topic

    article_md = generator.generate_page_update(
        page_url=page_url,
        mode=mode,
        current_title=current.get("title") or stored_title,
        current_meta=current.get("meta_description") or stored_meta,
        target_keyword=target_keyword,
        flaws=flaws,
        revision_notes=revision_notes,
    )
    proposed_title, proposed_meta = _extract_title_meta(article_md)

    full_md = _build_strategy_brief_md(page_url, mode, target_keyword, flaws) + article_md
    docx_path = docx_writer.save_article_as_docx(full_md, target_keyword)

    state_store.update_site(
        url,
        current_page_proposal={
            "page_url": page_url, "mode": mode, "target_keyword": target_keyword,
            "flaws": flaws, "proposed_title": proposed_title, "proposed_meta": proposed_meta,
        },
    )
    return docx_path, target_keyword


def generate_next_content_doc(url: str) -> tuple[dict, str] | None:
    """Pops the next page off the queue, generates its updated content
    (with a content-strategy brief prepended), saves it as a .docx, returns
    (site, docx_path). Returns None if the queue is empty (site should move
    to MONITORING).
    """
    site = state_store.get_site(url)
    queue = site["content_queue"]
    if not queue:
        return None

    item = queue[0]
    page_url = item["url"]
    docx_path, target_keyword = _generate_content_for_page(url, page_url, item["mode"], item.get("title"), item.get("meta_description"))

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


def regenerate_content_with_revision(url: str, revision_notes: str) -> tuple[dict, str]:
    """Redoes the *current* page's content (the one already sent, not yet
    popped further) incorporating the owner's revision request - actually
    acts on the feedback rather than just filing a note away.
    """
    site = state_store.get_site(url)
    proposal = site.get("current_page_proposal") or {}
    page_url = site.get("current_page_url")
    if not page_url:
        raise ValueError("No current page content to revise")

    docx_path, _ = _generate_content_for_page(
        url, page_url, proposal.get("mode", "REWRITE"), proposal.get("proposed_title"), proposal.get("proposed_meta"), revision_notes=revision_notes
    )
    return state_store.get_site(url), docx_path


def mark_sent_to_dev(url: str) -> dict:
    return state_store.update_site(url, stage="AWAITING_DEV_UPDATE")


def _verify_go_ahead(proposal: dict, on_page: dict) -> dict:
    """Checks whether the live page shows real evidence of the proposed
    update, instead of trusting a bare "go ahead" at face value. An exact
    title/meta match is one signal, but a developer might reword slightly -
    so this also re-checks whether the specific flaws the content was meant
    to fix (missing H1, no schema, thin content, etc.) are actually gone on
    the live page. If neither signal shows any change at all, that's a
    strong indicator nothing was actually published yet.
    """
    live_title = (on_page.get("title") or "").strip()
    live_meta = (on_page.get("meta_description") or "").strip()
    proposed_title = (proposal.get("proposed_title") or "").strip()
    proposed_meta = (proposal.get("proposed_meta") or "").strip()

    title_match = bool(proposed_title) and live_title == proposed_title
    meta_match = bool(proposed_meta) and live_meta == proposed_meta

    original_flaws = proposal.get("flaws") or []
    live_flaws = _compute_page_flaws(on_page)
    flaws_still_present = [f for f in original_flaws if f in live_flaws]
    flaws_resolved = [f for f in original_flaws if f not in live_flaws]

    if title_match or meta_match:
        status = "confirmed"
    elif original_flaws and flaws_resolved:
        status = "uncertain"
    else:
        status = "likely_not_updated"

    return {
        "status": status,
        "title_match": title_match,
        "meta_match": meta_match,
        "proposed_title": proposed_title,
        "live_title": live_title,
        "proposed_meta": proposed_meta,
        "live_meta": live_meta,
        "flaws_resolved": flaws_resolved,
        "flaws_still_present": flaws_still_present,
    }


def handle_go_ahead(url: str, force: bool = False) -> dict:
    """Called when the user tells the bot the developer has published the
    update. Re-crawls the *specific page* that was just implemented and
    checks for real evidence the proposed change actually went live (title/
    meta match, or the original flaws being resolved). If there's no
    evidence of any change, this does NOT silently continue to the next
    queued item - it returns blocked=True so the caller can surface a clear
    warning and require explicit confirmation, unless force=True.
    """
    site = state_store.get_site(url)
    audit_target = site.get("current_page_url") or url
    proposal = site.get("current_page_proposal") or {}
    audit = site_audit.full_audit(audit_target)
    on_page = audit.get("on_page") or {}

    verification = _verify_go_ahead(proposal, on_page) if proposal.get("page_url") == audit_target else None

    if verification and verification["status"] == "likely_not_updated" and not force:
        return {"site": site, "verification": verification, "blocked": True}

    report_md = _build_audit_report_md(audit_target, audit, verification)
    report_path = docx_writer.markdown_to_docx(report_md, "technical-seo-report.docx")

    if site["content_queue"]:
        state_store.update_site(url, stage="TECHNICAL_SEO_DONE")
    else:
        state_store.update_site(url, stage="MONITORING")

    return {"site": state_store.get_site(url), "report_path": report_path, "audit": audit, "verification": verification, "blocked": False}


def _build_audit_report_md(url: str, audit: dict, verification: dict | None = None) -> str:
    on_page = audit.get("on_page") or {}
    perf = audit.get("performance") or {}

    lines = [f"# Technical SEO Report - {url}", ""]

    if verification:
        status_label = {
            "confirmed": "CONFIRMED - live page matches what was proposed",
            "uncertain": "PARTIAL - some flaws resolved, but title/meta don't match exactly (developer may have reworded)",
            "likely_not_updated": "FORCED THROUGH - no evidence the proposed update is live, owner confirmed anyway",
        }[verification["status"]]
        lines.append("## Proposed vs Live Verification")
        lines.append(f"- Status: {status_label}")
        lines.append(f"- Title - proposed: \"{verification['proposed_title'] or '(none)'}\"")
        lines.append(f"- Title - live now: \"{verification['live_title'] or '(none)'}\"")
        lines.append(f"- Meta description - proposed: \"{verification['proposed_meta'] or '(none)'}\"")
        lines.append(f"- Meta description - live now: \"{verification['live_meta'] or '(none)'}\"")
        if verification["flaws_resolved"]:
            lines.append(f"- Flaws fixed: {', '.join(verification['flaws_resolved'])}")
        if verification["flaws_still_present"]:
            lines.append(f"- Flaws still present: {', '.join(verification['flaws_still_present'])}")
        lines.append("")

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


def check_monitoring(url: str) -> dict:
    """One periodic health/ranking check for a MONITORING-stage site. Re-audits
    a small sample of pages (the homepage plus pages this agent has already
    updated) using the same on-page checks used everywhere else in the app,
    and diffs the result against the last stored snapshot so only real
    changes get reported instead of a noisy restatement of steady state.

    Search Console is used opportunistically - only if it's configured
    globally (config.GSC_SITE_URL) and happens to match this exact site,
    since there's no per-site GSC connection flow yet. When it's not
    available, this relies entirely on crawl-based health signals, which
    need no extra setup.
    """
    site = state_store.get_site(url)
    sample_urls = list(dict.fromkeys([url] + (site.get("published_topics") or [])[-9:]))

    page_results = []
    for page_url in sample_urls:
        try:
            on_page = site_audit.audit_on_page(page_url)
            page_results.append({"url": page_url, "up": True, "flaws": _compute_page_flaws(on_page, use_fallback_message=False)})
        except Exception as e:
            page_results.append({"url": page_url, "up": False, "error": str(e)[:200], "flaws": []})

    gsc_snapshot = None
    if config.GSC_SITE_URL and config.GSC_SITE_URL.rstrip("/") == url.rstrip("/") and os.path.exists(config.GSC_SERVICE_ACCOUNT_FILE):
        try:
            gsc_snapshot = search_console.get_keyword_performance(days=28, row_limit=20)
        except Exception:
            gsc_snapshot = None  # best-effort only - GSC not connected/usable for this site

    previous_snapshot = site.get("monitoring_snapshot") or {}
    is_first_check = not previous_snapshot
    changes = [] if is_first_check else _diff_monitoring_snapshots(
        previous_snapshot.get("pages") or [], page_results, previous_snapshot.get("gsc"), gsc_snapshot
    )

    new_snapshot = {"checked_at": datetime.utcnow().isoformat(), "pages": page_results, "gsc": gsc_snapshot}
    state_store.update_site(url, monitoring_snapshot=new_snapshot)

    return {"changes": changes, "is_first_check": is_first_check, "gsc_available": gsc_snapshot is not None}


def _diff_monitoring_snapshots(previous_pages: list[dict], current_pages: list[dict], previous_gsc, current_gsc) -> list[str]:
    changes = []
    previous_by_url = {p["url"]: p for p in previous_pages}

    for current in current_pages:
        prev = previous_by_url.get(current["url"])
        if prev is None:
            continue  # first time this page has been sampled - nothing to diff against yet

        if prev.get("up") and not current.get("up"):
            changes.append(f"⚠️ {current['url']} ab live nahi hai ({current.get('error', 'unreachable')})")
        elif not prev.get("up") and current.get("up"):
            changes.append(f"✅ {current['url']} wapas live ho gaya hai")

        if current.get("up"):
            prev_flaws = set(prev.get("flaws") or [])
            current_flaws = set(current.get("flaws") or [])
            for f in current_flaws - prev_flaws:
                changes.append(f"⚠️ {current['url']}: naya issue - {f}")
            for f in prev_flaws - current_flaws:
                changes.append(f"✅ {current['url']}: fix ho gaya - {f}")

    if previous_gsc and current_gsc:
        prev_avg = sum(r["position"] for r in previous_gsc) / len(previous_gsc)
        curr_avg = sum(r["position"] for r in current_gsc) / len(current_gsc)
        if abs(curr_avg - prev_avg) >= 1:
            direction = "improve hui" if curr_avg < prev_avg else "gir gayi"
            changes.append(f"\U0001f4ca Average keyword position {direction}: {prev_avg:.1f} -> {curr_avg:.1f}")

    return changes
