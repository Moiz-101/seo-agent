"""Builds the agency-style audit PDF: cover, executive summary, score
breakdown + data-confidence transparency, priority action items, technical
SEO deep-dive, broken-link and image-alt evidence tables, competitor/keyword
snapshot (classified), screenshots, full page-by-page appendix, methodology.

Scoring is our own transparent composite (documented in the PDF itself so
it's never mistaken for a licensed/industry-standard number like Moz's):
Technical 30% + On-page 30% + Speed 25% + Authority 15%. Any component that
has no data (e.g. no PageSpeed key, no Open PageRank key) is left out and
the remaining weights are rescaled proportionally - the report always shows
a completeness percentage and confidence level so a partial score is never
mistaken for a full one.
"""
import io
import os
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # headless - no display available on the server
import matplotlib.pyplot as plt
import matplotlib.ticker
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
    PageBreak,
    HRFlowable,
    KeepTogether,
)

from config import DATA_DIR

REPORTS_DIR = os.path.join(DATA_DIR, "audit_reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

WEIGHTS = {"technical": 0.30, "on_page": 0.30, "speed": 0.25, "authority": 0.15}

NAVY = colors.HexColor("#0f172a")
BLUE = colors.HexColor("#2563eb")
GREEN = colors.HexColor("#16a34a")
AMBER = colors.HexColor("#d97706")
RED = colors.HexColor("#dc2626")
LIGHT_GREY = colors.HexColor("#f1f5f9")
MID_GREY = colors.HexColor("#64748b")

SEVERITY_COLOR = {"High": RED, "Medium": AMBER, "Low": MID_GREY}
SEVERITY_RANK = {"High": 0, "Medium": 1, "Low": 2}

COMPETITOR_CATEGORY_ORDER = ["Direct Business Competitor", "Organic SERP Competitor", "Directory/Aggregator", "Informational"]


def _hex(color_obj) -> str:
    return color_obj.hexval().replace("0x", "#")


def score_color(score):
    if score is None:
        return MID_GREY
    if score >= 80:
        return GREEN
    if score >= 50:
        return AMBER
    return RED


def compute_scores(crawl_stats: dict, authority: dict, performance_samples: list[dict]) -> dict:
    total = max(crawl_stats.get("total_pages_crawled", 0), 1)

    technical_issues = crawl_stats.get("broken_links", 0) + crawl_stats.get("non_indexable_pages", 0)
    technical = max(0.0, 100 * (1 - technical_issues / total))

    on_page_issue_slots = 4 * total  # title, meta, h1-missing, h1-multiple
    on_page_issues = (
        crawl_stats.get("pages_missing_title", 0)
        + crawl_stats.get("pages_missing_meta", 0)
        + crawl_stats.get("pages_missing_h1", 0)
        + crawl_stats.get("pages_multiple_h1", 0)
    )
    on_page = max(0.0, 100 * (1 - on_page_issues / max(on_page_issue_slots, 1)))

    valid_perf = [p["performance_score"] for p in performance_samples if p.get("performance_score") is not None]
    speed = round(100 * sum(valid_perf) / len(valid_perf), 1) if valid_perf else None

    auth_score = authority.get("authority_score_0_100") if authority.get("available") else None

    components = {"technical": round(technical, 1), "on_page": round(on_page, 1), "speed": speed, "authority": auth_score}

    available = {k: v for k, v in components.items() if v is not None}
    if available:
        weight_sum = sum(WEIGHTS[k] for k in available)
        overall = round(sum(v * WEIGHTS[k] / weight_sum for k, v in available.items()), 1)
    else:
        overall = None

    return {**components, "overall": overall}


def compute_data_confidence(authority: dict, performance_samples: list[dict]) -> dict:
    """Never let a partial audit pass itself off as a complete one: report
    exactly which sources are real/verified and which integrations are
    missing, plus a plain-language confidence level.
    """
    verified = ["Live site crawl (technical + on-page HTML analysis)", "DuckDuckGo autocomplete (seed keyword ideas)"]
    missing = []

    if any(p.get("performance_score") is not None for p in performance_samples):
        verified.append("Google PageSpeed Insights (speed + Core Web Vitals)")
    else:
        missing.append("Google PageSpeed Insights (speed score) - PAGESPEED_API_KEY not connected")

    if authority.get("available"):
        verified.append("Open PageRank (authority proxy)")
    else:
        missing.append("Open PageRank (authority score) - OPEN_PAGERANK_API_KEY not connected")

    missing.append("Google Search Console (real keyword rankings/impressions) - not connected for this site")
    missing.append("Paid backlink/rank-tracking tool (Ahrefs/Semrush/Moz/Majestic/DataForSEO) - not connected")

    total = len(verified) + len(missing)
    completeness_pct = round(100 * len(verified) / total) if total else 0
    confidence = "High" if completeness_pct >= 80 else "Medium" if completeness_pct >= 50 else "Low"

    return {
        "completeness_pct": completeness_pct,
        "confidence": confidence,
        "verified_sources": verified,
        "missing_integrations": missing,
    }


def _priority_issues(stats: dict, scores: dict, total_pages: int, duplicate_titles: list, duplicate_metas: list) -> list[dict]:
    issues = []

    def pct(n):
        return round(100 * n / max(total_pages, 1))

    if stats.get("broken_links", 0) > 0:
        n = stats["broken_links"]
        issues.append(
            {
                "severity": "High" if pct(n) > 5 else "Medium",
                "title": f"Fix {n} broken link{'s' if n != 1 else ''}",
                "detail": "Broken links waste crawl budget, hurt user trust, and can pass errors on to Google. See the Broken Links table for exact URLs.",
            }
        )
    if stats.get("pages_missing_h1", 0) > 0:
        n = stats["pages_missing_h1"]
        issues.append(
            {
                "severity": "High" if pct(n) > 20 else "Medium",
                "title": f"Add an H1 heading to {n} page{'s' if n != 1 else ''} ({pct(n)}% of the site)",
                "detail": "The H1 tells users and search engines what a page is about. Every indexable page should have exactly one clear, keyword-relevant H1.",
            }
        )
    if stats.get("pages_multiple_h1", 0) > 0:
        n = stats["pages_multiple_h1"]
        issues.append(
            {
                "severity": "Medium",
                "title": f"Reduce to a single H1 on {n} page{'s' if n != 1 else ''}",
                "detail": "Multiple H1 tags dilute topical focus. Keep one H1 per page and use H2/H3 for subheadings.",
            }
        )
    if stats.get("pages_missing_meta", 0) > 0:
        n = stats["pages_missing_meta"]
        issues.append(
            {
                "severity": "Medium",
                "title": f"Write meta descriptions for {n} page{'s' if n != 1 else ''}",
                "detail": "Without a meta description, Google auto-generates the search snippet - usually hurting click-through rate.",
            }
        )
    if duplicate_titles:
        n = sum(len(g["urls"]) for g in duplicate_titles)
        issues.append(
            {
                "severity": "Medium",
                "title": f"Fix {len(duplicate_titles)} duplicate title group{'s' if len(duplicate_titles) != 1 else ''} ({n} pages)",
                "detail": "Duplicate titles make it harder for Google to know which page to rank for a query. Give each page a unique, specific title.",
            }
        )
    if duplicate_metas:
        n = sum(len(g["urls"]) for g in duplicate_metas)
        issues.append(
            {
                "severity": "Low",
                "title": f"Fix {len(duplicate_metas)} duplicate meta description group{'s' if len(duplicate_metas) != 1 else ''} ({n} pages)",
                "detail": "Duplicate meta descriptions waste an opportunity to differentiate each page's search snippet.",
            }
        )
    if stats.get("orphan_pages", 0) > 0:
        n = stats["orphan_pages"]
        issues.append(
            {
                "severity": "Medium",
                "title": f"Link to {n} orphan page{'s' if n != 1 else ''} from somewhere on the site",
                "detail": "These pages were found (e.g. via sitemap) but have no internal links pointing to them, making them harder for both users and Google to discover.",
            }
        )
    if stats.get("mixed_protocol_links", 0) > 0:
        issues.append(
            {
                "severity": "Low",
                "title": f"Fix {stats['mixed_protocol_links']} link(s) still pointing to http:// instead of https://",
                "detail": "Mixed-protocol internal links can cause redirect hops and mixed-content warnings.",
            }
        )
    if stats.get("images_missing_alt_total", 0) > 0:
        n = stats["images_missing_alt_total"]
        issues.append(
            {
                "severity": "Low",
                "title": f"Add alt text to {n} image{'s' if n != 1 else ''}",
                "detail": "Alt text improves accessibility and gives search engines extra context for image search. See the Image Alt Text table.",
            }
        )
    if scores.get("speed") is not None and scores["speed"] < 70:
        issues.append(
            {
                "severity": "High" if scores["speed"] < 50 else "Medium",
                "title": f"Improve page speed (avg. {scores['speed']}/100)",
                "detail": "Slow pages hurt both search rankings and conversion rate - this is one of the highest-leverage fixes available.",
            }
        )
    if scores.get("authority") is None:
        issues.append(
            {
                "severity": "Low",
                "title": "Connect Open PageRank for an authority score",
                "detail": "Authority data isn't available yet for this audit - connecting it gives a fuller picture next time.",
            }
        )

    issues.sort(key=lambda i: SEVERITY_RANK[i["severity"]])
    return issues


def _generate_narrative(url: str, stats: dict, scores: dict, total_pages: int, confidence: dict) -> str:
    parts = []
    overall = scores.get("overall")
    if overall is not None:
        tier = "strong" if overall >= 80 else "a moderate" if overall >= 50 else "a weak"
        parts.append(
            f"{url} was crawled across {total_pages} pages and scores a provisional {overall}/100 overall "
            f"({confidence['completeness_pct']}% data completeness, {confidence['confidence'].lower()} confidence) - {tier} starting point."
        )
    else:
        parts.append(f"{url} was crawled across {total_pages} pages.")

    if stats.get("broken_links", 0) > 0:
        parts.append(f"{stats['broken_links']} broken link(s) were found, which waste crawl budget and can hurt user trust.")
    else:
        parts.append("No broken links were found in the crawled pages.")

    if stats.get("pages_missing_h1", 0):
        pct = round(100 * stats["pages_missing_h1"] / max(total_pages, 1))
        parts.append(
            f"{stats['pages_missing_h1']} pages ({pct}% of the site) are missing an H1 heading, one of the clearest "
            "on-page signals search engines use to understand page topics."
        )

    if stats.get("orphan_pages", 0):
        parts.append(f"{stats['orphan_pages']} page(s) appear to be orphaned (no internal links point to them).")

    if scores.get("speed") is not None:
        if scores["speed"] >= 80:
            parts.append(f"Page speed is healthy (avg. {scores['speed']}/100).")
        else:
            parts.append(f"Page speed needs attention (avg. {scores['speed']}/100) - this affects rankings and conversions.")
    else:
        parts.append("Page speed wasn't measured for this audit (PageSpeed Insights not connected).")

    return " ".join(parts)


def _score_chart(scores: dict) -> io.BytesIO:
    labels, values, bar_colors = [], [], []
    for key, label in [("technical", "Technical"), ("on_page", "On-Page"), ("speed", "Speed"), ("authority", "Authority")]:
        if scores.get(key) is not None:
            labels.append(label)
            values.append(scores[key])
            bar_colors.append(_hex(score_color(scores[key])))

    fig, ax = plt.subplots(figsize=(6.5, 3))
    bars = ax.barh(labels, values, color=bar_colors)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Score (0-100)")
    ax.set_title("Score Breakdown (provisional - see Data Confidence)")
    for bar, val in zip(bars, values):
        ax.text(val + 2, bar.get_y() + bar.get_height() / 2, f"{val}", va="center", fontweight="bold")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _issues_chart(stats: dict) -> io.BytesIO:
    issue_labels = {
        "broken_links": "Broken links",
        "pages_missing_meta": "Missing meta desc.",
        "pages_missing_h1": "Missing H1",
        "pages_multiple_h1": "Multiple H1",
        "non_indexable_pages": "Non-indexable",
        "orphan_pages": "Orphan pages",
    }
    labels = list(issue_labels.values())
    values = [stats.get(k, 0) for k in issue_labels]
    max_val = max(values)

    fig, ax = plt.subplots(figsize=(6.5, 3.5))
    ax.bar(labels, values, color="#dc2626")
    ax.set_ylabel("Pages affected")
    ax.set_title("Site Health Issues")
    ax.set_ylim(0, max(max_val, 1) * 1.2)
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    if max_val == 0:
        ax.text(0.5, 0.5, "No issues detected on crawled pages", transform=ax.transAxes, ha="center", va="center", fontsize=11, color="#16a34a")
    plt.xticks(rotation=30, ha="right")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LIGHT_GREY)
    canvas.line(0.6 * inch, 0.5 * inch, letter[0] - 0.6 * inch, 0.5 * inch)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MID_GREY)
    canvas.drawString(0.6 * inch, 0.35 * inch, "SEO Agent - Audit Report")
    canvas.drawRightString(letter[0] - 0.6 * inch, 0.35 * inch, f"Page {doc.page}")
    canvas.restoreState()


class _BookmarkedDocTemplate(SimpleDocTemplate):
    """Adds a PDF bookmarks/outline panel (most PDF viewers show this as a
    sidebar) for every Heading2 section, so the report has real, clickable
    section navigation - not just a manually-numbered table of contents.
    """

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "Heading2":
            text = flowable.getPlainText()
            key = f"section-{id(flowable)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=0, closed=False)


def _simple_table(rows, col_widths, font_size=9, header=True):
    t = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
        ]
    t.setStyle(TableStyle(style))
    return t


def build_audit_pdf(site_data: dict) -> str:
    """site_data keys: url, crawl ({'pages':.., 'stats':..}), authority (dict),
    performance_samples, screenshots, keyword_ideas, competitors (classified
    list of {domain, category, keyword_matches}), technical_checks,
    broken_links_report, image_alt_findings, duplicate_titles, duplicate_metas.
    """
    url = site_data["url"]
    crawl_pages = site_data["crawl"]["pages"]
    crawl_stats = site_data["crawl"]["stats"]
    authority = site_data["authority"]
    performance_samples = site_data["performance_samples"]
    screenshots = site_data.get("screenshots", [])
    keyword_ideas = site_data.get("keyword_ideas", [])
    competitors = site_data.get("competitors", [])
    technical_checks = site_data.get("technical_checks", {})
    broken_links_report = site_data.get("broken_links_report", {"broken_links": [], "extra_checks_capped": False})
    image_alt_findings = site_data.get("image_alt_findings", [])
    duplicate_titles = site_data.get("duplicate_titles", [])
    duplicate_metas = site_data.get("duplicate_metas", [])
    total_pages = crawl_stats.get("total_pages_crawled", 0)

    scores = compute_scores(crawl_stats, authority, performance_samples)
    confidence = compute_data_confidence(authority, performance_samples)

    safe_name = "".join(c if c.isalnum() else "-" for c in url).strip("-")
    path = os.path.join(REPORTS_DIR, f"audit-{safe_name}.pdf")

    doc = _BookmarkedDocTemplate(path, pagesize=letter, topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    styles = getSampleStyleSheet()
    h2 = styles["Heading2"]
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=15)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, leading=11)
    white_title = ParagraphStyle("WhiteTitle", parent=styles["Title"], textColor=colors.white, fontSize=24, alignment=0)
    white_sub = ParagraphStyle("WhiteSub", parent=styles["BodyText"], textColor=colors.white, fontSize=11)
    card_value = ParagraphStyle("CardValue", parent=styles["Title"], fontSize=22, alignment=1, textColor=colors.white, spaceAfter=2)
    card_label = ParagraphStyle("CardLabel", parent=styles["BodyText"], fontSize=8, alignment=1, textColor=colors.white)

    story = []

    # --- Cover banner ---
    banner = Table(
        [[Paragraph("SEO AUDIT REPORT", white_title)], [Paragraph(url, white_sub)], [Paragraph(datetime.utcnow().strftime("%d %B %Y"), white_sub)]],
        colWidths=[6.9 * inch],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("TOPPADDING", (0, 0), (-1, 0), 22),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 22),
                ("LEFTPADDING", (0, 0), (-1, -1), 20),
                ("TOPPADDING", (0, 1), (-1, 2), 2),
            ]
        )
    )
    story.append(banner)
    story.append(Spacer(1, 20))

    # --- Stat cards ---
    def card(label, value, bg):
        t = Table([[Paragraph(str(value), card_value)], [Paragraph(label, card_label)]], colWidths=[1.62 * inch])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("TOPPADDING", (0, 0), (-1, 0), 14), ("BOTTOMPADDING", (0, -1), (-1, -1), 12)]))
        return t

    overall_display = f"{scores['overall']}/100" if scores["overall"] is not None else "N/A"
    cards = Table(
        [
            [
                card("Provisional Score", overall_display, score_color(scores["overall"])),
                card("Pages Crawled", total_pages, BLUE),
                card("Data Completeness", f"{confidence['completeness_pct']}%", score_color(confidence["completeness_pct"])),
                card("Broken Links", crawl_stats.get("broken_links", 0), GREEN if crawl_stats.get("broken_links", 0) == 0 else RED),
            ]
        ],
        colWidths=[1.72 * inch] * 4,
        spaceBefore=0,
    )
    cards.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    story.append(cards)
    story.append(Spacer(1, 24))

    # --- Executive summary ---
    story.append(Paragraph("Executive Summary", h2))
    story.append(Paragraph(_generate_narrative(url, crawl_stats, scores, total_pages, confidence), body))
    story.append(Spacer(1, 16))

    # --- Data confidence ---
    story.append(Paragraph("Data Confidence", h2))
    conf_lines = [f"<b>Confidence level: {confidence['confidence']}</b> ({confidence['completeness_pct']}% of possible data sources connected)"]
    story.append(Paragraph(conf_lines[0], body))
    story.append(Spacer(1, 4))
    story.append(Paragraph("<b>Verified sources used in this audit:</b>", body))
    for s in confidence["verified_sources"]:
        story.append(Paragraph(f"&bull; {s}", small))
    story.append(Spacer(1, 4))
    story.append(Paragraph("<b>Not connected (would improve accuracy):</b>", body))
    for m in confidence["missing_integrations"]:
        story.append(Paragraph(f"&bull; {m}", small))
    story.append(Spacer(1, 16))

    # --- Priority action items ---
    story.append(Paragraph("Priority Action Items", h2))
    issues = _priority_issues(crawl_stats, scores, total_pages, duplicate_titles, duplicate_metas)
    if issues:
        rows = [["Priority", "Recommendation"]]
        for issue in issues:
            badge = Paragraph(f'<font color="{_hex(SEVERITY_COLOR[issue["severity"]])}"><b>{issue["severity"]}</b></font>', body)
            detail_cell = Paragraph(f"<b>{issue['title']}</b><br/>{issue['detail']}", body)
            rows.append([badge, detail_cell])
        story.append(_simple_table(rows, [0.9 * inch, 5.3 * inch], font_size=9))
    else:
        story.append(Paragraph("No priority issues found - the site is in good technical/on-page health.", body))
    story.append(Spacer(1, 16))

    # --- Charts ---
    story.append(Paragraph("Score Breakdown & Site Health", h2))
    story.append(Image(_score_chart(scores), width=6.3 * inch, height=2.9 * inch))
    story.append(Spacer(1, 8))
    story.append(Image(_issues_chart(crawl_stats), width=6.3 * inch, height=3.4 * inch))
    story.append(Spacer(1, 16))

    # --- Technical SEO deep-dive ---
    story.append(Paragraph("Technical SEO Findings", h2))
    tech_rows = [
        ["Check", "Result"],
        ["robots.txt present", "Yes" if technical_checks.get("robots_txt_present") else "No"],
        ["Sitemap declared in robots.txt", "Yes" if technical_checks.get("sitemap_declared_in_robots") else "No"],
        ["sitemap.xml present", "Yes" if technical_checks.get("sitemap_xml_present") else "No"],
        ["URLs listed in sitemap.xml", str(technical_checks.get("sitemap_url_count", "N/A"))],
        ["Pages missing canonical tag", str(sum(1 for p in crawl_pages if p.get("status_code") == 200 and not p.get("canonical")))],
        ["Non-indexable pages (noindex)", str(crawl_stats.get("non_indexable_pages", 0))],
        ["Duplicate title groups", str(len(duplicate_titles))],
        ["Duplicate meta description groups", str(len(duplicate_metas))],
        ["Orphan pages (no internal links in)", str(crawl_stats.get("orphan_pages", 0))],
        ["Max crawl depth reached", str(crawl_stats.get("max_crawl_depth", 0))],
        ["Pages with structured data (schema.org)", f"{total_pages - crawl_stats.get('pages_missing_schema', 0)} / {total_pages}"],
        ["Images missing width/height (CLS risk)", str(crawl_stats.get("images_missing_dimensions_total", 0))],
        ["Mixed http/https internal links", str(crawl_stats.get("mixed_protocol_links", 0))],
    ]
    valid_perf = [p for p in performance_samples if p.get("performance_score") is not None]
    if valid_perf:
        avg_lcp_sample = next((p["largest_contentful_paint"] for p in valid_perf if p.get("largest_contentful_paint")), "N/A")
        avg_cls_sample = next((p["cumulative_layout_shift"] for p in valid_perf if p.get("cumulative_layout_shift")), "N/A")
        tech_rows += [
            ["Mobile performance score (sample avg)", f"{scores.get('speed', 'N/A')}/100"],
            ["Largest Contentful Paint (sample)", str(avg_lcp_sample)],
            ["Cumulative Layout Shift (sample)", str(avg_cls_sample)],
        ]
    else:
        tech_rows.append(["Core Web Vitals / mobile performance", "Not measured (PageSpeed Insights not connected)"])

    story.append(_simple_table(tech_rows, [3.8 * inch, 2.4 * inch], font_size=9))
    story.append(Spacer(1, 12))

    if duplicate_titles:
        dup_block = [Paragraph("Duplicate Titles (examples)", styles["Heading3"])]
        for group in duplicate_titles[:5]:
            dup_block.append(Paragraph(f"<b>“{group['value']}”</b> used on {len(group['urls'])} pages:", small))
            for u in group["urls"][:5]:
                dup_block.append(Paragraph(f"&bull; {u}", small))
        story.append(KeepTogether(dup_block))
        story.append(Spacer(1, 12))

    if crawl_stats.get("orphan_pages", 0) > 0:
        orphan_urls = [p["url"] for p in crawl_pages if p.get("is_orphan")][:10]
        orphan_block = [Paragraph("Orphan Pages (examples)", styles["Heading3"])]
        for u in orphan_urls:
            orphan_block.append(Paragraph(f"&bull; {u}", small))
        story.append(KeepTogether(orphan_block))
    story.append(Spacer(1, 16))

    # --- Broken links table ---
    story.append(Paragraph("Broken Links", h2))
    broken = broken_links_report.get("broken_links", [])
    if broken:
        rows = [["Source Page", "Broken Destination", "Status", "Type", "Anchor Text", "Recommended Fix"]]
        for b in broken[:40]:
            rows.append(
                [
                    Paragraph(b["source_page"], small),
                    Paragraph(b["destination"], small),
                    str(b["status"]),
                    b["type"],
                    Paragraph(b["anchor_text"][:60], small),
                    Paragraph(b["recommended_fix"], small),
                ]
            )
        story.append(_simple_table(rows, [1.3 * inch, 1.3 * inch, 0.5 * inch, 0.55 * inch, 1.1 * inch, 1.45 * inch], font_size=7))
        if broken_links_report.get("extra_checks_capped"):
            story.append(Spacer(1, 4))
            story.append(Paragraph("Link-checking was capped for audit speed - there may be more broken links beyond what's listed here.", small))
    else:
        story.append(Paragraph("No broken links found among the links checked.", body))
    story.append(Spacer(1, 16))

    # --- Image alt text table ---
    story.append(Paragraph("Image Alt Text Findings", h2))
    if image_alt_findings:
        rows = [["Page URL", "Image URL", "Classification", "Priority", "Recommended Alt"]]
        for i in image_alt_findings[:40]:
            rows.append(
                [
                    Paragraph(i["page_url"], small),
                    Paragraph(i["image_url"], small),
                    i["classification"],
                    i["priority"],
                    Paragraph(i["recommended_alt"], small),
                ]
            )
        story.append(_simple_table(rows, [1.5 * inch, 1.5 * inch, 0.8 * inch, 0.6 * inch, 1.8 * inch], font_size=7))
    else:
        story.append(Paragraph("No images missing alt text were found.", body))
    story.append(PageBreak())

    # --- Keyword & competitor snapshot ---
    kw_block = [Paragraph("Keyword Opportunities", h2)]
    if keyword_ideas:
        kw_block.append(Paragraph("<b>AI-derived seed keywords</b> — search volume and difficulty unavailable (no paid keyword tool connected):", body))
        kw_block.append(Spacer(1, 6))
        kw_rows = [["Keyword idea"]] + [[Paragraph(k["keyword"], body)] for k in keyword_ideas[:12]]
        kw_block.append(_simple_table(kw_rows, [6.2 * inch], font_size=9))
    else:
        kw_block.append(Paragraph("No keyword suggestions could be generated for this site right now.", body))
    story.append(KeepTogether(kw_block))
    story.append(Spacer(1, 20))

    comp_block = [Paragraph("Competitor Snapshot", h2)]
    if competitors:
        comp_block.append(
            Paragraph(
                "Classified from free DuckDuckGo search results (approximate). Directories/aggregators and informational "
                "sites are shown separately and are never treated as direct competitors.",
                body,
            )
        )
        comp_block.append(Spacer(1, 6))
        by_category: dict[str, list[dict]] = {}
        for c in competitors:
            by_category.setdefault(c["category"], []).append(c)
        for category in COMPETITOR_CATEGORY_ORDER:
            entries = by_category.get(category)
            if not entries:
                continue
            comp_block.append(Paragraph(f"<b>{category}</b> ({len(entries)})", styles["Heading3"]))
            for e in entries[:8]:
                comp_block.append(Paragraph(f"&bull; {e['domain']}", small))
    else:
        comp_block.append(Paragraph("No competitor data could be gathered for this site right now.", body))
    story.append(KeepTogether(comp_block))
    story.append(Spacer(1, 16))

    # --- Screenshots ---
    if screenshots:
        for idx, (label, img_bytes) in enumerate(screenshots):
            heading = [Paragraph("Screenshots", h2)] if idx == 0 else []
            story.append(
                KeepTogether(
                    heading
                    + [
                        Paragraph(label, body),
                        Image(io.BytesIO(img_bytes), width=6.0 * inch, height=3.7 * inch),
                        Spacer(1, 14),
                    ]
                )
            )
    story.append(PageBreak())

    # --- Page-by-page audit appendix ---
    story.append(Paragraph("Page-by-Page Audit Appendix", h2))
    story.append(Paragraph(f"Full evidence for all {total_pages} crawled pages.", body))
    story.append(Spacer(1, 6))
    appendix_rows = [["URL", "Status", "Idx", "Title (len)", "Meta", "H1", "Words", "Int.Links", "Alt Miss", "Canon", "Issues"]]
    for p in crawl_pages:
        status = p.get("status_code")
        status_display = str(status) if status is not None else "unreachable"
        if status != 200:
            appendix_rows.append([Paragraph(p["url"], small), status_display, "-", "-", "-", "-", "-", "-", "-", "-", "fetch failed"])
            continue

        page_issues = []
        if not p.get("title"):
            page_issues.append("no title")
        if not p.get("meta_description"):
            page_issues.append("no meta")
        if p.get("h1_count") == 0:
            page_issues.append("no H1")
        elif p.get("h1_count", 1) > 1:
            page_issues.append("multi-H1")
        if not p.get("canonical"):
            page_issues.append("no canonical")
        if p.get("is_orphan"):
            page_issues.append("orphan")

        appendix_rows.append(
            [
                Paragraph(p["url"], small),
                status_display,
                "Y" if p.get("indexable", True) else "N",
                f"{p.get('title_length', 0)}",
                "Y" if p.get("meta_description") else "N",
                str(p.get("h1_count", 0)),
                str(p.get("word_count", 0)),
                str(p.get("internal_link_count", 0)),
                str(p.get("images_missing_alt", 0)),
                "Y" if p.get("canonical") else "N",
                Paragraph(", ".join(page_issues) or "none", small),
            ]
        )
    story.append(
        _simple_table(
            appendix_rows,
            [1.7 * inch, 0.5 * inch, 0.3 * inch, 0.55 * inch, 0.4 * inch, 0.3 * inch, 0.45 * inch, 0.5 * inch, 0.45 * inch, 0.4 * inch, 1.0 * inch],
            font_size=6.5,
        )
    )
    if crawl_stats.get("crawl_capped"):
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"This site has more than {total_pages} pages - the crawl was capped for audit speed. This appendix covers the crawled sample only.", small))

    story.append(PageBreak())

    # --- Methodology + next step ---
    story.append(HRFlowable(width="100%", color=LIGHT_GREY))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Methodology", h2))
    story.append(
        Paragraph(
            "Overall Site Score = Technical 30% + On-Page 30% + Speed 25% + Authority 15%. It is always labelled "
            "<b>provisional</b> because it only uses the data sources listed under Data Confidence above; any score "
            "component without data is excluded and the remaining weights are rescaled rather than inflating the "
            "final number. Authority uses Open PageRank as a free proxy for Domain Authority (not Moz's proprietary "
            "metric). Keyword ideas are AI-derived from free autocomplete data, not real search volume/ranking "
            "numbers, which require Search Console or a paid keyword tool.",
            body,
        )
    )
    story.append(Spacer(1, 14))
    story.append(Paragraph("Next Step", h2))
    story.append(
        Paragraph(
            "Reply on Telegram with one of: <b>Approve Audit</b>, <b>Request Revision</b>, <b>Scratch Start</b> "
            "(to begin optimizing this site's existing pages - no new pages are ever created without your say-so), "
            "or <b>Pause</b>.",
            body,
        )
    )

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path
