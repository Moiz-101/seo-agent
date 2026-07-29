"""Builds the agency-style audit PDF: cover, score breakdown, charts,
screenshots, page-health issues, recommendations.

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
)

from config import DATA_DIR

REPORTS_DIR = os.path.join(DATA_DIR, "audit_reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

WEIGHTS = {"technical": 0.30, "on_page": 0.30, "speed": 0.25, "authority": 0.15}


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


def _score_chart(scores: dict) -> io.BytesIO:
    labels, values = [], []
    for key, label in [("technical", "Technical"), ("on_page", "On-Page"), ("speed", "Speed"), ("authority", "Authority")]:
        if scores.get(key) is not None:
            labels.append(label)
            values.append(scores[key])

    fig, ax = plt.subplots(figsize=(6, 2.5))
    bars = ax.barh(labels, values, color="#2563eb")
    ax.set_xlim(0, 100)
    ax.set_xlabel("Score (0-100)")
    ax.set_title("Score Breakdown")
    for bar, val in zip(bars, values):
        ax.text(val + 2, bar.get_y() + bar.get_height() / 2, f"{val}", va="center")
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
    labels = [v for k, v in issue_labels.items()]
    values = [stats.get(k, 0) for k in issue_labels]
    max_val = max(values)

    fig, ax = plt.subplots(figsize=(6, 3))
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


def build_audit_pdf(site_data: dict) -> str:
    """site_data keys: url, crawl ({'pages':.., 'stats':..}), authority (dict),
    performance_samples (list of dicts with url + PSI fields), screenshots
    (list of (label, png_bytes) tuples).
    """
    url = site_data["url"]
    crawl_stats = site_data["crawl"]["stats"]
    authority = site_data["authority"]
    performance_samples = site_data["performance_samples"]
    screenshots = site_data.get("screenshots", [])

    scores = compute_scores(crawl_stats, authority, performance_samples)

    safe_name = "".join(c if c.isalnum() else "-" for c in url).strip("-")
    path = os.path.join(REPORTS_DIR, f"audit-{safe_name}.pdf")

    doc = SimpleDocTemplate(path, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleBig", parent=styles["Title"], fontSize=26)
    score_style = ParagraphStyle(
        "ScoreBig", parent=styles["Title"], fontSize=48, leading=56, spaceAfter=12, textColor=colors.HexColor("#2563eb")
    )
    h2 = styles["Heading2"]
    body = styles["BodyText"]

    story = []

    # Cover
    story.append(Paragraph("SEO Audit Report", title_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph(url, h2))
    story.append(Paragraph(datetime.utcnow().strftime("%d %B %Y"), body))
    story.append(Spacer(1, 24))
    if scores["overall"] is not None:
        story.append(Paragraph(f"{scores['overall']}<font size=18>/100</font>", score_style))
        story.append(Paragraph("Overall Site Score (our own composite - see methodology below)", body))
    story.append(PageBreak())

    # Summary
    story.append(Paragraph("Summary", h2))
    summary_rows = [
        ["Pages crawled", str(crawl_stats.get("total_pages_crawled", 0)) + (" (capped)" if crawl_stats.get("crawl_capped") else "")],
        ["Broken links", str(crawl_stats.get("broken_links", 0))],
        ["Pages missing title", str(crawl_stats.get("pages_missing_title", 0))],
        ["Pages missing meta description", str(crawl_stats.get("pages_missing_meta", 0))],
        ["Pages missing H1", str(crawl_stats.get("pages_missing_h1", 0))],
        ["Average word count", str(crawl_stats.get("avg_word_count", 0))],
        [
            "Authority Score (Open PageRank proxy, not Moz DA)",
            f"{authority.get('authority_score_0_10')}/10" if authority.get("available") else "not connected",
        ],
        [
            "Keyword rankings",
            "connect Search Console for real ranking data" if True else "",
        ],
    ]
    table = Table([["Metric", "Value"]] + summary_rows, colWidths=[3.2 * inch, 3 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 20))

    # Charts
    story.append(Image(_score_chart(scores), width=5.5 * inch, height=2.3 * inch))
    story.append(Spacer(1, 10))
    story.append(Image(_issues_chart(crawl_stats), width=5.5 * inch, height=2.75 * inch))
    story.append(PageBreak())

    # Screenshots
    if screenshots:
        story.append(Paragraph("Screenshots", h2))
        for label, img_bytes in screenshots:
            story.append(Paragraph(label, body))
            story.append(Image(io.BytesIO(img_bytes), width=5.5 * inch, height=3.4 * inch))
            story.append(Spacer(1, 14))
        story.append(PageBreak())

    # Top issues detail
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
            issues = []
            if not p.get("title"):
                issues.append("missing title")
            if not p.get("meta_description"):
                issues.append("missing meta description")
            if p.get("h1_count") == 0:
                issues.append("missing H1")
            elif p.get("h1_count", 1) > 1:
                issues.append("multiple H1")
            rows.append([Paragraph(p["url"], body), ", ".join(issues)])
        issues_table = Table(rows, colWidths=[4 * inch, 2.2 * inch])
        issues_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(issues_table)
    else:
        story.append(Paragraph("No major on-page issues found in the crawled pages.", body))
    story.append(Spacer(1, 20))

    # Methodology + recommendations
    story.append(Paragraph("Methodology", h2))
    story.append(
        Paragraph(
            "Overall Site Score = Technical 30% + On-Page 30% + Speed 25% + Authority 15%. "
            "Authority uses Open PageRank as a free proxy for Domain Authority (not Moz's "
            "proprietary metric). Any component without data is excluded and the remaining "
            "weights are rescaled.",
            body,
        )
    )
    story.append(Spacer(1, 12))
    story.append(Paragraph("Next Step", h2))
    story.append(
        Paragraph(
            "Reply <b>Scratch Start</b> on Telegram to begin optimizing this site's existing "
            "pages (no new pages are created until you say so).",
            body,
        )
    )

    doc.build(story)
    return path
