"""Builds the agency-style audit PDF: cover, executive summary, score
breakdown, priority action items, keyword/competitor snapshot, screenshots,
detailed issues, methodology.

Scoring is our own transparent composite (documented in the PDF itself so
it's never mistaken for a licensed/industry-standard number like Moz's):
Technical 30% + On-page 30% + Speed 25% + Authority 15%. Any component that
has no data (e.g. no PageSpeed key, no Open PageRank key) is left out and
the remaining weights are rescaled proportionally.
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


def _priority_issues(stats: dict, scores: dict, total_pages: int) -> list[dict]:
    issues = []

    def pct(n):
        return round(100 * n / max(total_pages, 1))

    if stats.get("broken_links", 0) > 0:
        n = stats["broken_links"]
        issues.append(
            {
                "severity": "High" if pct(n) > 5 else "Medium",
                "title": f"Fix {n} broken link{'s' if n != 1 else ''}",
                "detail": "Broken links waste crawl budget, hurt user trust, and can pass errors on to Google. Fix or 301-redirect these URLs.",
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
    if stats.get("images_missing_alt_total", 0) > 0:
        n = stats["images_missing_alt_total"]
        issues.append(
            {
                "severity": "Low",
                "title": f"Add alt text to {n} image{'s' if n != 1 else ''}",
                "detail": "Alt text improves accessibility and gives search engines extra context for image search.",
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


def _generate_narrative(url: str, stats: dict, scores: dict, total_pages: int) -> str:
    parts = []
    overall = scores.get("overall")
    if overall is not None:
        tier = "strong" if overall >= 80 else "a moderate" if overall >= 50 else "a weak"
        parts.append(f"{url} was crawled across {total_pages} pages and scores {overall}/100 overall - {tier} starting point.")
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

    if stats.get("pages_missing_meta", 0):
        parts.append(f"{stats['pages_missing_meta']} page(s) have no meta description.")

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
    ax.set_title("Score Breakdown")
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
        "pages_missing_title": "Missing title",
        "pages_missing_meta": "Missing meta desc.",
        "pages_missing_h1": "Missing H1",
        "pages_multiple_h1": "Multiple H1",
        "non_indexable_pages": "Non-indexable",
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


def build_audit_pdf(site_data: dict) -> str:
    """site_data keys: url, crawl ({'pages':.., 'stats':..}), authority (dict),
    performance_samples (list of dicts with url + PSI fields), screenshots
    (list of (label, png_bytes) tuples), keyword_ideas (list of dicts),
    competitors (list of domain strings).
    """
    url = site_data["url"]
    crawl_stats = site_data["crawl"]["stats"]
    authority = site_data["authority"]
    performance_samples = site_data["performance_samples"]
    screenshots = site_data.get("screenshots", [])
    keyword_ideas = site_data.get("keyword_ideas", [])
    competitors = site_data.get("competitors", [])
    total_pages = crawl_stats.get("total_pages_crawled", 0)

    scores = compute_scores(crawl_stats, authority, performance_samples)

    safe_name = "".join(c if c.isalnum() else "-" for c in url).strip("-")
    path = os.path.join(REPORTS_DIR, f"audit-{safe_name}.pdf")

    doc = SimpleDocTemplate(path, pagesize=letter, topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    styles = getSampleStyleSheet()
    h2 = styles["Heading2"]
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=15)
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
                card("Overall Score", overall_display, score_color(scores["overall"])),
                card("Pages Crawled", total_pages, BLUE),
                card("Broken Links", crawl_stats.get("broken_links", 0), GREEN if crawl_stats.get("broken_links", 0) == 0 else RED),
                card("Avg. Word Count", crawl_stats.get("avg_word_count", 0), MID_GREY),
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
    story.append(Paragraph(_generate_narrative(url, crawl_stats, scores, total_pages), body))
    story.append(Spacer(1, 16))

    # --- Priority action items ---
    story.append(Paragraph("Priority Action Items", h2))
    issues = _priority_issues(crawl_stats, scores, total_pages)
    if issues:
        rows = [["Priority", "Recommendation"]]
        row_colors = [colors.white]
        for issue in issues:
            badge = Paragraph(f'<font color="{_hex(SEVERITY_COLOR[issue["severity"]])}"><b>{issue["severity"]}</b></font>', body)
            detail_cell = Paragraph(f"<b>{issue['title']}</b><br/>{issue['detail']}", body)
            rows.append([badge, detail_cell])
        issues_table = Table(rows, colWidths=[0.9 * inch, 5.3 * inch])
        issues_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(issues_table)
    else:
        story.append(Paragraph("No priority issues found - the site is in good technical/on-page health.", body))
    story.append(Spacer(1, 16))

    # --- Charts ---
    story.append(Paragraph("Score Breakdown & Site Health", h2))
    story.append(Image(_score_chart(scores), width=6.3 * inch, height=2.9 * inch))
    story.append(Spacer(1, 8))
    story.append(Image(_issues_chart(crawl_stats), width=6.3 * inch, height=3.4 * inch))
    story.append(PageBreak())  # charts + text below reflow awkwardly if packed on one page

    # --- Keyword & competitor snapshot ---
    kw_block = [Paragraph("Keyword Opportunities", h2)]
    if keyword_ideas:
        kw_block.append(Paragraph("Free-tier keyword signal (DuckDuckGo autocomplete) based on this site's homepage topic:", body))
        kw_block.append(Spacer(1, 6))
        kw_rows = [["Keyword idea"]] + [[Paragraph(k["keyword"], body)] for k in keyword_ideas[:12]]
        kw_table = Table(kw_rows, colWidths=[6.2 * inch])
        kw_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
                ]
            )
        )
        kw_block.append(kw_table)
    else:
        kw_block.append(Paragraph("No keyword suggestions could be generated for this site right now.", body))
    story.append(KeepTogether(kw_block))
    story.append(Spacer(1, 20))

    comp_block = [Paragraph("Competitor Snapshot", h2)]
    if competitors:
        comp_block.append(Paragraph("Domains showing up for this site's likely topic (free DuckDuckGo search, approximate):", body))
        comp_block.append(Spacer(1, 6))
        for c in competitors[:8]:
            comp_block.append(Paragraph(f"&bull; {c}", body))
    else:
        comp_block.append(Paragraph("No competitor data could be gathered for this site right now.", body))
    story.append(KeepTogether(comp_block))
    story.append(Spacer(1, 16))

    # --- Screenshots ---
    if screenshots:
        story.append(Paragraph("Screenshots", h2))
        for label, img_bytes in screenshots:
            story.append(
                KeepTogether(
                    [
                        Paragraph(label, body),
                        Image(io.BytesIO(img_bytes), width=6.0 * inch, height=3.7 * inch),
                        Spacer(1, 14),
                    ]
                )
            )

    # --- Detailed issues table ---
    story.append(Paragraph("Pages With Issues (sample)", h2))
    problem_pages = [
        p
        for p in site_data["crawl"]["pages"]
        if p.get("status_code") == 200
        and (not p.get("title") or not p.get("meta_description") or p.get("h1_count") != 1)
    ][:20]
    if problem_pages:
        rows = [["URL", "Issue"]]
        for p in problem_pages:
            page_issues = []
            if not p.get("title"):
                page_issues.append("missing title")
            if not p.get("meta_description"):
                page_issues.append("missing meta description")
            if p.get("h1_count") == 0:
                page_issues.append("missing H1")
            elif p.get("h1_count", 1) > 1:
                page_issues.append("multiple H1")
            rows.append([Paragraph(p["url"], body), ", ".join(page_issues)])
        detail_table = Table(rows, colWidths=[4.3 * inch, 1.9 * inch])
        detail_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
                ]
            )
        )
        story.append(detail_table)
    else:
        story.append(Paragraph("No major on-page issues found in the crawled pages.", body))
    story.append(Spacer(1, 20))

    # --- Methodology + next step ---
    story.append(HRFlowable(width="100%", color=LIGHT_GREY))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Methodology", h2))
    story.append(
        Paragraph(
            "Overall Site Score = Technical 30% + On-Page 30% + Speed 25% + Authority 15%. "
            "Authority uses Open PageRank as a free proxy for Domain Authority (not Moz's "
            "proprietary metric). Keyword rankings require Search Console to be connected for "
            "this site - without it, keyword ideas above come from free autocomplete data "
            "instead of real ranking/volume numbers. Any score component without data is "
            "excluded and the remaining weights are rescaled.",
            body,
        )
    )
    story.append(Spacer(1, 14))
    story.append(Paragraph("Next Step", h2))
    story.append(
        Paragraph(
            "Reply <b>Scratch Start</b> on Telegram to begin optimizing this site's existing "
            "pages (no new pages are created until you say so).",
            body,
        )
    )

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path
