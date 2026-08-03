"""Builds the agency-style audit PDF: cover, table of contents, executive
summary, data-confidence transparency, priority action items, technical SEO
deep-dive, evidence summaries (full detail lives in the companion XLSX
package - see xlsx_export.py), classified keyword/competitor snapshot,
screenshots, recommended action plan, methodology.

Scoring is our own transparent composite: Technical 30% + On-page 30% +
Speed 25% + Authority 15%. It is always labelled PROVISIONAL - any missing
component is excluded and the remaining weights rescaled, which can raise
or lower the number depending on what's missing, so it is explicitly not
presented as a complete SEO-health score. See Data Confidence for exactly
what was and wasn't measured.
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
KEYWORD_CATEGORY_LABELS = {
    "relevant_non_branded": "Relevant (non-branded)",
    "informational": "Informational",
    "commercial": "Commercial intent",
    "local": "Local intent",
}


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


def compute_scores(crawl_stats: dict, authority: dict, performance_samples: list[dict], broken_links_report: dict) -> dict:
    total = max(crawl_stats.get("total_pages_crawled", 0), 1)

    unique_broken = broken_links_report.get("unique_internal_broken_urls", 0)
    technical_issues = unique_broken + crawl_stats.get("non_indexable_pages", 0)
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


def _build_issues(stats, scores, total_pages, duplicate_titles, duplicate_metas, broken_links_report, protocol_issues, schema_report) -> list[dict]:
    """Every issue gets severity + owner + effort + expected impact +
    verification method, so the same list can render both the concise
    Priority Action Items table and the fuller Recommended Action Plan.
    """
    issues = []

    def pct(n):
        return round(100 * n / max(total_pages, 1))

    unique_internal = broken_links_report.get("unique_internal_broken_urls", 0)
    occurrences = broken_links_report.get("internal_broken_occurrences", 0)
    affected = broken_links_report.get("affected_source_pages", 0)
    if unique_internal:
        issues.append(
            {
                "severity": "High" if pct(unique_internal) > 5 else "Medium",
                "title": f"Fix {unique_internal} broken internal URL{'s' if unique_internal != 1 else ''} ({occurrences} link occurrences across {affected} pages)",
                "detail": "Broken links waste crawl budget, hurt user trust, and can pass errors on to Google. Full list in broken-links.xlsx.",
                "owner": "Developer", "effort": "Medium", "affected_pages": affected,
                "impact": "Recovers crawl budget and lost link equity; improves user trust",
                "verification": "Re-crawl and confirm the destinations resolve with a 200 status",
            }
        )
    unique_external = broken_links_report.get("unique_external_unreachable_urls", 0)
    if unique_external:
        issues.append(
            {
                "severity": "Low", "title": f"Review {unique_external} external link(s) that were unreachable or returned an error",
                "detail": "Some may be temporarily down or blocking automated checks rather than genuinely broken - see broken-links.xlsx for the failure reason on each.",
                "owner": "Content Team", "effort": "Low", "affected_pages": broken_links_report.get("affected_source_pages", 0),
                "impact": "Keeps outbound references trustworthy",
                "verification": "Manually open each link in a browser to confirm",
            }
        )
    if stats.get("pages_missing_h1", 0) > 0:
        n = stats["pages_missing_h1"]
        issues.append(
            {
                "severity": "High" if pct(n) > 20 else "Medium",
                "title": f"Add an H1 heading to {n} page{'s' if n != 1 else ''} ({pct(n)}% of the site)",
                "detail": "The H1 tells users and search engines what a page is about.",
                "owner": "Content Team", "effort": "Low", "affected_pages": n,
                "impact": "Clearer topical signal to search engines for affected pages",
                "verification": "Re-crawl and confirm h1_count = 1 in page-audit.xlsx",
            }
        )
    if stats.get("pages_multiple_h1", 0) > 0:
        n = stats["pages_multiple_h1"]
        issues.append(
            {
                "severity": "Medium", "title": f"Reduce to a single H1 on {n} page{'s' if n != 1 else ''}",
                "detail": "Multiple H1 tags dilute topical focus.",
                "owner": "Content Team", "effort": "Low", "affected_pages": n,
                "impact": "Sharper topical focus per page", "verification": "Re-crawl and confirm h1_count = 1",
            }
        )
    if stats.get("pages_missing_meta", 0) > 0:
        n = stats["pages_missing_meta"]
        issues.append(
            {
                "severity": "Medium", "title": f"Write meta descriptions for {n} page{'s' if n != 1 else ''}",
                "detail": "Without one, Google auto-generates the search snippet - usually hurting click-through rate.",
                "owner": "Content Team", "effort": "Low", "affected_pages": n,
                "impact": "Better-controlled, more compelling search snippets",
                "verification": "Re-crawl and confirm meta_description is present",
            }
        )
    if duplicate_titles:
        n = sum(len(g["urls"]) for g in duplicate_titles)
        issues.append(
            {
                "severity": "Medium", "title": f"Fix {len(duplicate_titles)} duplicate title group{'s' if len(duplicate_titles) != 1 else ''} ({n} pages)",
                "detail": "Duplicate titles make it harder for Google to know which page to rank for a query.",
                "owner": "Content Team", "effort": "Low", "affected_pages": n,
                "impact": "Reduces keyword cannibalisation risk", "verification": "Re-crawl and confirm titles are unique",
            }
        )
    if duplicate_metas:
        n = sum(len(g["urls"]) for g in duplicate_metas)
        issues.append(
            {
                "severity": "Low", "title": f"Fix {len(duplicate_metas)} duplicate meta description group{'s' if len(duplicate_metas) != 1 else ''} ({n} pages)",
                "detail": "Wastes an opportunity to differentiate each page's search snippet.",
                "owner": "Content Team", "effort": "Low", "affected_pages": n,
                "impact": "More distinct search snippets", "verification": "Re-crawl and confirm meta descriptions are unique",
            }
        )
    protocol_dups = protocol_issues.get("protocol_duplicates", [])
    if protocol_dups:
        issues.append(
            {
                "severity": "High", "title": f"Fix {len(protocol_dups)} http/https duplicate-content page(s)",
                "detail": "These pages serve identical content on both http:// and https:// with no redirect between them - a real duplicate-content/canonicalization issue, not just a duplicate title.",
                "owner": "Developer", "effort": "Medium", "affected_pages": len(protocol_dups),
                "impact": "Consolidates ranking signals onto a single canonical URL",
                "verification": "Confirm the http:// version 301-redirects to https://",
            }
        )
    if stats.get("orphan_pages", 0) > 0:
        n = stats["orphan_pages"]
        issues.append(
            {
                "severity": "Medium", "title": f"Link to {n} orphan page{'s' if n != 1 else ''} from somewhere on the site",
                "detail": "Found (e.g. via sitemap) but nothing links to them internally.",
                "owner": "SEO Team", "effort": "Low", "affected_pages": n,
                "impact": "Improves discoverability and crawl efficiency",
                "verification": "Re-crawl and confirm incoming_internal_links > 0",
            }
        )
    if stats.get("images_missing_alt_total", 0) > 0:
        n = stats["images_missing_alt_total"]
        issues.append(
            {
                "severity": "Low", "title": f"Add alt text to images missing it ({n} occurrences sitewide)",
                "detail": "See image-alt-audit.xlsx for deduplicated, AI-drafted suggestions per unique image.",
                "owner": "Content Team", "effort": "Low", "affected_pages": n,
                "impact": "Accessibility + extra image-search context",
                "verification": "Re-crawl and confirm images_missing_alt = 0",
            }
        )
    if schema_report.get("recommendations"):
        issues.append(
            {
                "severity": "Low", "title": "Add missing structured data (schema.org)",
                "detail": "; ".join(schema_report["recommendations"]),
                "owner": "Developer", "effort": "Medium", "affected_pages": total_pages - schema_report.get("pages_with_schema", 0),
                "impact": "Improves eligibility for rich search results",
                "verification": "Re-crawl and confirm schema_present = true with valid JSON-LD",
            }
        )
    if scores.get("speed") is not None and scores["speed"] < 70:
        issues.append(
            {
                "severity": "High" if scores["speed"] < 50 else "Medium",
                "title": f"Improve page speed (avg. {scores['speed']}/100)",
                "detail": "Slow pages hurt both search rankings and conversion rate.",
                "owner": "Developer", "effort": "High", "affected_pages": total_pages,
                "impact": "Better rankings and lower bounce rate",
                "verification": "Re-run PageSpeed Insights and confirm score improvement",
            }
        )
    if scores.get("authority") is None:
        issues.append(
            {
                "severity": "Low", "title": "Connect Open PageRank for an authority score",
                "detail": "Not connected for this audit.",
                "owner": "SEO Team", "effort": "Low", "affected_pages": 0,
                "impact": "More complete provisional score next audit",
                "verification": "Confirm authority.available = true on next run",
            }
        )

    for i in issues:
        i.setdefault("status", "Not Started")
    issues.sort(key=lambda i: SEVERITY_RANK[i["severity"]])
    return issues


def _generate_narrative(url, stats, scores, total_pages, confidence, broken_links_report) -> str:
    parts = []
    overall = scores.get("overall")
    if overall is not None:
        tier = "strong" if overall >= 80 else "a moderate" if overall >= 50 else "a weak"
        parts.append(
            f"{url} was crawled across {total_pages} pages and scores a provisional {overall}/100 overall "
            f"({confidence['completeness_pct']}% data completeness, {confidence['confidence'].lower()} confidence) - {tier} starting point. "
            "This is not a complete SEO-health score - see Data Confidence for exactly what was measured."
        )
    else:
        parts.append(f"{url} was crawled across {total_pages} pages.")

    unique_broken = broken_links_report.get("unique_internal_broken_urls", 0)
    if unique_broken:
        occ = broken_links_report.get("internal_broken_occurrences", 0)
        affected = broken_links_report.get("affected_source_pages", 0)
        parts.append(f"{unique_broken} unique internal URL(s) are broken, linked to {occ} times from {affected} pages.")
    else:
        parts.append("No confirmed broken internal links were found.")

    if stats.get("pages_missing_h1", 0):
        pct = round(100 * stats["pages_missing_h1"] / max(total_pages, 1))
        parts.append(f"{stats['pages_missing_h1']} pages ({pct}% of the site) are missing an H1 heading.")

    if stats.get("orphan_pages", 0):
        parts.append(f"{stats['orphan_pages']} page(s) appear to be orphaned (no internal links point to them).")

    if scores.get("speed") is not None:
        parts.append(f"Page speed {'is healthy' if scores['speed'] >= 80 else 'needs attention'} (avg. {scores['speed']}/100).")
    else:
        parts.append("Page speed wasn't measured (PageSpeed Insights not connected).")

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


def _issues_chart(stats: dict, broken_unique: int) -> io.BytesIO:
    labels = ["Broken URLs\n(unique)", "Missing meta\ndesc.", "Missing H1", "Multiple H1", "Non-indexable", "Orphan pages"]
    values = [broken_unique, stats.get("pages_missing_meta", 0), stats.get("pages_missing_h1", 0), stats.get("pages_multiple_h1", 0), stats.get("non_indexable_pages", 0), stats.get("orphan_pages", 0)]
    max_val = max(values)

    fig, ax = plt.subplots(figsize=(6.5, 3.5))
    ax.bar(labels, values, color="#dc2626")
    ax.set_ylabel("Pages / URLs affected")
    ax.set_title("Site Health Issues")
    ax.set_ylim(0, max(max_val, 1) * 1.2)
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    if max_val == 0:
        ax.text(0.5, 0.5, "No issues detected on crawled pages", transform=ax.transAxes, ha="center", va="center", fontsize=11, color="#16a34a")
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
    """Adds a PDF bookmarks/outline panel for every numbered section, so
    the report has real, clickable navigation."""

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
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
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
    """site_data keys (see pipeline.run_audit): url, crawl, authority,
    performance_samples, screenshots, keyword_ideas (classified), competitors
    (classified + LLM-verified), technical_checks, broken_links_report,
    image_alt_findings (deduplicated), duplicate_titles, duplicate_metas,
    protocol_issues, redirects_report, schema_report.
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
    broken_links_report = site_data.get("broken_links_report", {})
    image_alt_findings = site_data.get("image_alt_findings", [])
    duplicate_titles = site_data.get("duplicate_titles", [])
    duplicate_metas = site_data.get("duplicate_metas", [])
    protocol_issues = site_data.get("protocol_issues", {"protocol_duplicates": []})
    schema_report = site_data.get("schema_report", {"pages_with_schema": 0, "pages_total": 0, "schema_types_found": {}, "pages_with_invalid_schema": [], "recommendations": []})
    total_pages = crawl_stats.get("total_pages_crawled", 0)

    scores = compute_scores(crawl_stats, authority, performance_samples, broken_links_report)
    confidence = compute_data_confidence(authority, performance_samples)
    issues = _build_issues(crawl_stats, scores, total_pages, duplicate_titles, duplicate_metas, broken_links_report, protocol_issues, schema_report)

    safe_name = "".join(c if c.isalnum() else "-" for c in url).strip("-")
    path = os.path.join(REPORTS_DIR, f"audit-{safe_name}.pdf")

    doc = _BookmarkedDocTemplate(path, pagesize=letter, topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    styles = getSampleStyleSheet()
    h2 = styles["Heading2"]
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10.5, leading=15.5)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8.5, leading=12)
    white_title = ParagraphStyle("WhiteTitle", parent=styles["Title"], textColor=colors.white, fontSize=26, alignment=0)
    white_sub = ParagraphStyle("WhiteSub", parent=styles["BodyText"], textColor=colors.white, fontSize=11.5)
    card_value = ParagraphStyle("CardValue", parent=styles["Title"], fontSize=22, alignment=1, textColor=colors.white, spaceAfter=2)
    card_label = ParagraphStyle("CardLabel", parent=styles["BodyText"], fontSize=8.5, alignment=1, textColor=colors.white)
    toc_style = ParagraphStyle("Toc", parent=styles["BodyText"], fontSize=11, leading=20)

    section_counter = [0]

    def section(title: str) -> Paragraph:
        section_counter[0] += 1
        return Paragraph(f"{section_counter[0]}. {title}", h2)

    story = []

    # --- Cover banner ---
    banner = Table(
        [
            [Paragraph("SEO AUDIT REPORT", white_title)],
            [Paragraph(url, white_sub)],
            [Paragraph(datetime.utcnow().strftime("%d %B %Y"), white_sub)],
            [Paragraph(f"Provisional Score: {scores['overall']}/100 &nbsp;&nbsp;|&nbsp;&nbsp; Data Completeness: {confidence['completeness_pct']}%" if scores["overall"] is not None else "Provisional Score: N/A", white_sub)],
        ],
        colWidths=[6.9 * inch],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("TOPPADDING", (0, 0), (-1, 0), 24),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 24),
                ("LEFTPADDING", (0, 0), (-1, -1), 20),
                ("TOPPADDING", (0, 1), (-1, 3), 3),
            ]
        )
    )
    story.append(banner)
    story.append(Spacer(1, 20))

    def card(label, value, bg):
        t = Table([[Paragraph(str(value), card_value)], [Paragraph(label, card_label)]], colWidths=[1.62 * inch])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("TOPPADDING", (0, 0), (-1, 0), 14), ("BOTTOMPADDING", (0, -1), (-1, -1), 12)]))
        return t

    unique_broken = broken_links_report.get("unique_internal_broken_urls", 0)
    cards = Table(
        [
            [
                card("Provisional Score", f"{scores['overall']}/100" if scores["overall"] is not None else "N/A", score_color(scores["overall"])),
                card("Pages Crawled", total_pages, BLUE),
                card("Data Completeness", f"{confidence['completeness_pct']}%", score_color(confidence["completeness_pct"])),
                card("Broken URLs (unique)", unique_broken, GREEN if unique_broken == 0 else RED),
            ]
        ],
        colWidths=[1.72 * inch] * 4,
    )
    cards.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    story.append(cards)
    story.append(PageBreak())

    # --- Table of contents ---
    toc_entries = [
        "1. Executive Summary", "2. Data Confidence", "3. Priority Action Items",
        "4. Score Breakdown & Site Health", "5. Technical SEO Findings", "6. Broken Links",
        "7. Image Alt Text", "8. Keyword Opportunities", "9. Competitor Snapshot",
        "10. Screenshots", "11. Page-by-Page Summary", "12. Recommended Action Plan",
        "13. Methodology & Attached Files",
    ]
    story.append(Paragraph("Table of Contents", h2))
    for entry in toc_entries:
        story.append(Paragraph(entry, toc_style))
    story.append(PageBreak())

    # --- 1. Executive summary ---
    story.append(section("Executive Summary"))
    story.append(Paragraph(_generate_narrative(url, crawl_stats, scores, total_pages, confidence, broken_links_report), body))
    story.append(Spacer(1, 16))

    # --- 2. Data confidence ---
    story.append(section("Data Confidence"))
    story.append(
        Paragraph(
            f"<b>Confidence level: {confidence['confidence']}</b> ({confidence['completeness_pct']}% of possible data sources connected). "
            "<font color=\"#dc2626\"><b>The provisional score above is not a complete SEO-health score</b></font> - "
            "it only reflects the sources listed below.",
            body,
        )
    )
    story.append(Spacer(1, 4))
    story.append(Paragraph("<b>Verified sources used in this audit:</b>", body))
    for s in confidence["verified_sources"]:
        story.append(Paragraph(f"&bull; {s}", small))
    story.append(Spacer(1, 4))
    story.append(Paragraph("<b>Not connected (would improve accuracy):</b>", body))
    for m in confidence["missing_integrations"]:
        story.append(Paragraph(f"&bull; {m}", small))
    story.append(Spacer(1, 16))

    # --- 3. Priority action items ---
    story.append(section("Priority Action Items"))
    if issues:
        rows = [["Priority", "Recommendation"]]
        for issue in issues:
            badge = Paragraph(f'<font color="{_hex(SEVERITY_COLOR[issue["severity"]])}"><b>{issue["severity"]}</b></font>', body)
            detail_cell = Paragraph(f"<b>{issue['title']}</b><br/>{issue['detail']}", body)
            rows.append([badge, detail_cell])
        story.append(_simple_table(rows, [0.9 * inch, 5.3 * inch], font_size=9.5))
    else:
        story.append(Paragraph("No priority issues found - the site is in good technical/on-page health.", body))
    story.append(Spacer(1, 16))

    # --- 4. Charts ---
    story.append(section("Score Breakdown & Site Health"))
    story.append(Image(_score_chart(scores), width=6.3 * inch, height=2.9 * inch))
    story.append(Spacer(1, 8))
    story.append(Image(_issues_chart(crawl_stats, unique_broken), width=6.3 * inch, height=3.4 * inch))
    story.append(Spacer(1, 16))

    # --- 5. Technical SEO deep-dive ---
    story.append(section("Technical SEO Findings"))
    tech_rows = [
        ["Check", "Result"],
        ["robots.txt present", "Yes" if technical_checks.get("robots_txt_present") else "No"],
        ["Sitemap declared in robots.txt", "Yes" if technical_checks.get("sitemap_declared_in_robots") else "No"],
        ["sitemap.xml present", "Yes" if technical_checks.get("sitemap_xml_present") else "No"],
        ["URLs listed in sitemap.xml", str(technical_checks.get("sitemap_url_count", "N/A"))],
        ["Pages missing canonical tag", str(sum(1 for p in crawl_pages if p.get("status_code") == 200 and not p.get("canonical")))],
        ["Non-indexable pages (noindex)", str(crawl_stats.get("non_indexable_pages", 0))],
        ["HTTP/HTTPS duplicate-content pages", str(len(protocol_issues.get("protocol_duplicates", [])))],
        ["Duplicate title groups", str(len(duplicate_titles))],
        ["Duplicate meta description groups", str(len(duplicate_metas))],
        ["Orphan pages (no internal links in)", str(crawl_stats.get("orphan_pages", 0))],
        ["Max crawl depth reached", str(crawl_stats.get("max_crawl_depth", 0))],
        ["Pages with structured data (schema.org)", f"{schema_report.get('pages_with_schema', 0)} / {schema_report.get('pages_total', total_pages)}"],
        ["Schema types found", ", ".join(schema_report.get("schema_types_found", {}).keys()) or "none"],
        ["Pages with invalid/unparseable schema", str(len(schema_report.get("pages_with_invalid_schema", [])))],
        ["Images missing width/height (CLS risk)", str(crawl_stats.get("images_missing_dimensions_total", 0))],
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
    story.append(_simple_table(tech_rows, [3.6 * inch, 2.6 * inch], font_size=9.5))
    story.append(Spacer(1, 12))

    if protocol_issues.get("protocol_duplicates"):
        pd_block = [Paragraph("HTTP/HTTPS Duplicate-Content Pages", styles["Heading3"])]
        for d in protocol_issues["protocol_duplicates"][:5]:
            pd_block.append(Paragraph(f"&bull; {d['http_url']} (serves 200 directly - not redirected) vs. {d['https_url']}", small))
        story.append(KeepTogether(pd_block))
        story.append(Spacer(1, 10))

    if duplicate_titles:
        dup_block = [Paragraph("Duplicate Titles (examples)", styles["Heading3"])]
        for group in duplicate_titles[:4]:
            dup_block.append(Paragraph(f"<b>“{group['value']}”</b> used on {len(group['urls'])} pages: " + ", ".join(group["urls"][:3]), small))
        story.append(KeepTogether(dup_block))
        story.append(Spacer(1, 10))

    if crawl_stats.get("orphan_pages", 0) > 0:
        orphan_urls = [p["url"] for p in crawl_pages if p.get("is_orphan")][:8]
        orphan_block = [Paragraph("Orphan Pages (examples)", styles["Heading3"])]
        for u in orphan_urls:
            orphan_block.append(Paragraph(f"&bull; {u}", small))
        story.append(KeepTogether(orphan_block))
    story.append(PageBreak())

    # --- 6. Broken links summary ---
    story.append(section("Broken Links"))
    confirmed = broken_links_report.get("confirmed_broken", [])
    unverified = broken_links_report.get("unverified", [])
    summary_rows = [
        ["Metric", "Count"],
        ["Unique internal broken URLs", str(broken_links_report.get("unique_internal_broken_urls", 0))],
        ["Total internal broken-link occurrences", str(broken_links_report.get("internal_broken_occurrences", 0))],
        ["Unique external unreachable URLs", str(broken_links_report.get("unique_external_unreachable_urls", 0))],
        ["Pages affected (link to a broken/unreachable destination)", str(broken_links_report.get("affected_source_pages", 0))],
        ["Unverified (timeout/DNS/SSL/likely bot-blocked)", str(len(unverified))],
    ]
    story.append(_simple_table(summary_rows, [4.2 * inch, 2.0 * inch], font_size=9.5))
    story.append(Spacer(1, 10))

    if confirmed:
        seen_dest = set()
        sample = []
        for b in confirmed:
            if b["destination"] not in seen_dest:
                seen_dest.add(b["destination"])
                sample.append(b)
            if len(sample) >= 12:
                break
        rows = [["Source Page", "Broken Destination", "Status", "Suggested Redirect"]]
        for b in sample:
            rows.append([Paragraph(b["source_page"], small), Paragraph(b["destination"], small), str(b["status"]), Paragraph(b.get("suggested_redirect") or "-", small)])
        story.append(Paragraph(f"Sample of {len(sample)} unique broken destinations (full {len(confirmed)}-row detail, including every source page, is in <b>broken-links.xlsx</b>):", body))
        story.append(Spacer(1, 4))
        story.append(_simple_table(rows, [1.7 * inch, 1.9 * inch, 0.6 * inch, 1.9 * inch], font_size=8))
    else:
        story.append(Paragraph("No confirmed broken links found among the links checked.", body))
    if broken_links_report.get("extra_checks_capped"):
        story.append(Spacer(1, 4))
        story.append(Paragraph("Link-checking was capped for audit speed - there may be more unchecked links beyond this sample.", small))
    story.append(PageBreak())

    # --- 7. Image alt text summary ---
    story.append(section("Image Alt Text"))
    meaningful = [f for f in image_alt_findings if f["classification"] == "Meaningful"]
    decorative = [f for f in image_alt_findings if f["classification"] == "Decorative"]
    total_occurrences = sum(f["occurrence_count"] for f in image_alt_findings)
    img_summary_rows = [
        ["Metric", "Count"],
        ["Unique images missing alt text", str(len(image_alt_findings))],
        ["Total occurrences across all pages", str(total_occurrences)],
        ["Meaningful (content) images", str(len(meaningful))],
        ["Decorative / sitewide-reused images (logos, icons)", str(len(decorative))],
    ]
    story.append(_simple_table(img_summary_rows, [4.2 * inch, 2.0 * inch], font_size=9.5))
    story.append(Spacer(1, 10))
    if meaningful:
        rows = [["Image URL", "Occurrences", "AI-Suggested Alt"]]
        for f in meaningful[:12]:
            rows.append([Paragraph(f["image_url"], small), str(f["occurrence_count"]), Paragraph(f.get("recommended_alt") or "", small)])
        story.append(Paragraph(f"Sample of {min(12, len(meaningful))} meaningful images (full detail in <b>image-alt-audit.xlsx</b>):", body))
        story.append(Spacer(1, 4))
        story.append(_simple_table(rows, [2.4 * inch, 0.9 * inch, 2.8 * inch], font_size=8))
    else:
        story.append(Paragraph("No meaningful (non-decorative) images are missing alt text.", body))
    story.append(PageBreak())

    # --- 8. Keyword opportunities ---
    kw_block = [section("Keyword Opportunities")]
    non_branded = [k for k in keyword_ideas if k.get("category") not in ("competitor_branded",)]
    competitor_branded = [k for k in keyword_ideas if k.get("category") == "competitor_branded"]
    if non_branded:
        kw_block.append(Paragraph("<b>AI-derived seed keywords</b> - search volume and difficulty unavailable (no paid keyword tool connected). Competitor-branded terms are excluded below (shown separately) since they aren't your own organic targeting opportunities.", body))
        kw_block.append(Spacer(1, 6))
        kw_rows = [["Keyword idea", "Category"]] + [
            [Paragraph(k["keyword"], body), KEYWORD_CATEGORY_LABELS.get(k.get("category"), k.get("category", "own_branded").replace("_", " ").title())]
            for k in non_branded[:15]
        ]
        kw_block.append(_simple_table(kw_rows, [4.4 * inch, 1.8 * inch], font_size=9.5))
    else:
        kw_block.append(Paragraph("No keyword suggestions could be generated for this site right now.", body))
    story.append(KeepTogether(kw_block))
    story.append(Spacer(1, 16))

    if competitor_branded:
        cb_block = [Paragraph("Competitor-Branded Terms Found (excluded from your keyword list above)", styles["Heading3"])]
        for k in competitor_branded[:8]:
            cb_block.append(Paragraph(f"&bull; {k['keyword']} (matches: {k.get('competitor_match', '?')})", small))
        story.append(KeepTogether(cb_block))
    story.append(PageBreak())

    # --- 9. Competitor snapshot ---
    comp_block = [section("Competitor Snapshot")]
    if competitors:
        comp_block.append(
            Paragraph(
                "Classified from free DuckDuckGo search results, with the top 'Direct Business Competitor' "
                "candidates spot-checked by AI against their homepage content (downgraded if they look like a "
                "directory, tool, or unrelated site the domain blocklist missed). Directories, aggregators, and "
                "informational sites are never treated as direct competitors.",
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
                note = " (downgraded after AI review)" if e.get("verification_note") else ""
                comp_block.append(Paragraph(f"&bull; {e['domain']}{note}", small))
    else:
        comp_block.append(Paragraph("No competitor data could be gathered for this site right now.", body))
    story.append(KeepTogether(comp_block))
    story.append(Spacer(1, 16))

    # --- 10. Screenshots ---
    if screenshots:
        for idx, (label, img_bytes) in enumerate(screenshots):
            heading = [section("Screenshots")] if idx == 0 else []
            story.append(KeepTogether(heading + [Paragraph(label, body), Image(io.BytesIO(img_bytes), width=6.0 * inch, height=3.7 * inch), Spacer(1, 14)]))
    else:
        story.append(section("Screenshots"))
        story.append(Paragraph("No screenshots could be captured for this audit.", body))
    story.append(PageBreak())

    # --- 11. Page-by-page summary ---
    story.append(section("Page-by-Page Summary"))
    story.append(
        Paragraph(
            f"{total_pages} pages were crawled. Full per-page evidence (title, meta, H1, word count, links, images, "
            "canonical, and every detected issue for each page) is in the attached <b>page-audit.xlsx</b> - kept out of "
            "this PDF to keep it readable.",
            body,
        )
    )
    story.append(Spacer(1, 10))
    problem_pages = [p for p in crawl_pages if p.get("status_code") == 200 and (not p.get("title") or not p.get("meta_description") or p.get("h1_count") != 1)][:10]
    if problem_pages:
        rows = [["URL", "Key Issue"]]
        for p in problem_pages:
            i = []
            if not p.get("title"):
                i.append("no title")
            if not p.get("meta_description"):
                i.append("no meta")
            if p.get("h1_count") != 1:
                i.append("H1 issue")
            rows.append([Paragraph(p["url"], small), ", ".join(i)])
        story.append(Paragraph(f"Worst {len(problem_pages)} pages by on-page issue count:", body))
        story.append(Spacer(1, 4))
        story.append(_simple_table(rows, [4.4 * inch, 1.8 * inch], font_size=9))
    if crawl_stats.get("crawl_capped"):
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"This site has more than {total_pages} pages - the crawl was capped for audit speed.", small))
    story.append(PageBreak())

    # --- 12. Recommended action plan ---
    story.append(section("Recommended Action Plan"))
    if issues:
        rows = [["Issue", "Priority", "Pages", "Owner", "Effort", "Expected Impact", "Verification", "Status"]]
        for i in issues:
            rows.append(
                [
                    Paragraph(i["title"], small), i["severity"], str(i.get("affected_pages", "-")), i["owner"], i["effort"],
                    Paragraph(i["impact"], small), Paragraph(i["verification"], small), i["status"],
                ]
            )
        story.append(_simple_table(rows, [1.7 * inch, 0.55 * inch, 0.4 * inch, 0.65 * inch, 0.5 * inch, 1.3 * inch, 1.3 * inch, 0.6 * inch], font_size=7))
    else:
        story.append(Paragraph("No outstanding issues to plan for.", body))
    story.append(PageBreak())

    # --- 13. Methodology + attached files ---
    story.append(HRFlowable(width="100%", color=LIGHT_GREY))
    story.append(Spacer(1, 10))
    story.append(section("Methodology & Attached Files"))
    story.append(
        Paragraph(
            "Overall Site Score = Technical 30% + On-Page 30% + Speed 25% + Authority 15%, always labelled "
            "<b>provisional</b>. When a component has no data, it is excluded and the remaining weights are "
            "rescaled among the available components - this can raise <i>or</i> lower the resulting number "
            "depending on which component is missing, so it is not presented as a complete SEO-health score. "
            "See Data Confidence for the exact sources used. Authority uses Open PageRank as a free proxy for "
            "Domain Authority (not Moz's proprietary metric). Structured-data checking validates that JSON-LD "
            "parses correctly and extracts @type values - it is not a full schema.org spec validator. Competitor "
            "and keyword classification use frequency heuristics plus an AI spot-check, not a paid SERP-overlap tool.",
            body,
        )
    )
    story.append(Spacer(1, 12))
    story.append(Paragraph("<b>Attached files:</b> broken-links.xlsx, image-alt-audit.xlsx, page-audit.xlsx, redirects.xlsx, metadata.xlsx", body))
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
