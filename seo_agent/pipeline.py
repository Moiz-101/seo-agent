"""Per-website SEO pipeline state machine.

Stages:
  RESEARCH            -> gathering keywords/competitors/on-page audit
  CONTENT_READY        -> a content doc has been generated and sent to Telegram,
                          waiting for the user to hand it to the developer
  AWAITING_DEV_UPDATE   -> user confirmed doc was sent; waiting for "go ahead"
                          (i.e. developer has published it on the live site)
  TECHNICAL_SEO         -> after go-ahead, run on-page/technical audit on the
                          now-updated page and produce a fix-it report
  MONITORING            -> content queue exhausted; periodically checks GSC
                          rankings until the user stops the site
  STOPPED               -> user said stop
"""
from seo_agent.storage import state_store
from seo_agent.research import keywords as kw_research
from seo_agent.research import competitors as competitor_research
from seo_agent.research import site_audit
from seo_agent.content import generator, docx_writer


def start_new_site(url: str, seed_topics: list[str]) -> dict:
    site = state_store.create_site(url)
    return run_research(url, seed_topics)


def run_research(url: str, seed_topics: list[str]) -> dict:
    raw_keywords = kw_research.research_keywords(seed_topics)
    clusters = kw_research.cluster_keywords(raw_keywords)

    if not clusters:
        # Google Trends is unreliable from some networks/cloud IPs and can return
        # nothing at all. Fall back to the seed topics themselves so the pipeline
        # still produces content instead of silently generating zero articles.
        clusters = [[{"keyword": topic, "source": "seed_topic", "signal": 0}] for topic in seed_topics]

    top_keywords = [k["keyword"] for k in raw_keywords[:10]] or seed_topics
    competitors = competitor_research.top_competitors_for_keywords(top_keywords, url)

    content_queue = [
        generator.generate_content_brief(url, cluster) for cluster in clusters if cluster
    ]

    site = state_store.update_site(
        url,
        stage="RESEARCH_DONE",
        keywords=raw_keywords,
        competitors=competitors,
        content_queue=content_queue,
    )
    return site


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
