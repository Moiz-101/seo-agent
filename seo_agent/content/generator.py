"""Content generation via Google Gemini (free tier)."""
import google.generativeai as genai

from config import GEMINI_API_KEY

genai.configure(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-flash-latest"  # alias that Google keeps pointed at the current flash model

CONTENT_PROMPT = """You are an expert SEO content writer. Write a complete, publish-ready
article optimized for the primary keyword below.

Website: {domain}
Primary keyword: {primary_keyword}
Related/secondary keywords to naturally include: {secondary_keywords}
Target audience/intent: {intent}

Requirements:
- Compelling SEO title (under 60 characters)
- Meta description (under 155 characters)
- 1 H1, multiple H2/H3 subheadings
- 1000-1500 words, natural keyword usage (no keyword stuffing)
- Short paragraphs, scannable, include a brief intro and conclusion with a call to action
- Suggest 2-3 internal linking anchor text ideas (topics, not URLs) at the end under "Internal Link Suggestions"

Output in clean Markdown with clear ## Title, ## Meta Description, then the article body.
"""


def generate_article(domain: str, primary_keyword: str, secondary_keywords: list[str], intent: str = "informational") -> str:
    model = genai.GenerativeModel(MODEL_NAME)
    prompt = CONTENT_PROMPT.format(
        domain=domain,
        primary_keyword=primary_keyword,
        secondary_keywords=", ".join(secondary_keywords),
        intent=intent,
    )
    response = model.generate_content(prompt)
    return response.text


def generate_content_brief(domain: str, cluster: list[dict]) -> dict:
    """Turns a keyword cluster into a single content brief (primary + secondary kws)."""
    sorted_cluster = sorted(cluster, key=lambda k: k.get("signal", 0), reverse=True)
    primary = sorted_cluster[0]["keyword"]
    secondary = [k["keyword"] for k in sorted_cluster[1:6]]
    return {"domain": domain, "primary_keyword": primary, "secondary_keywords": secondary}


FACT_GUARDRAIL = (
    "Do not invent specific business facts that weren't given to you - certifications, "
    "awards, years in business, customer counts, prices, guarantees, or testimonials. "
    "Where real business information is needed but not provided, use a placeholder like "
    "[ADD SPECIFIC DETAIL] instead of making something up."
)

REWRITE_PROMPT = """You are an expert SEO content writer rewriting an existing website page
that currently has weak search visibility (no meaningful Search Console ranking data yet).

Page URL: {page_url}
Current title: {current_title}
Current meta description: {current_meta}
Target keyword: {target_keyword}
Known issues with the current page: {flaws}
{revision_section}
Write a complete replacement for this page's content, optimized for the target keyword and
addressing the known issues above.

Requirements:
- Compelling SEO title (under 60 characters)
- Meta description (under 155 characters)
- 1 H1, multiple H2/H3 subheadings
- Natural keyword usage (no keyword stuffing)
- Short paragraphs, scannable, with a clear call to action
- Suggest 2-3 internal linking anchor text ideas (topics, not URLs) under "Internal Link Suggestions"
- {fact_guardrail}

Output in clean Markdown with clear ## Title, ## Meta Description, then the page body.
"""

POLISH_PROMPT = """You are an expert SEO content editor improving an existing page that
already has some search visibility. Preserve what's working; sharpen what's weak - this is
an edit, not a full rewrite.

Page URL: {page_url}
Current title: {current_title}
Current meta description: {current_meta}
Target keyword: {target_keyword}
Known issues with the current page: {flaws}
{revision_section}
Produce an improved version of this page's content: tighten the title and meta description,
strengthen headings, close obvious content gaps for the target keyword, and improve the call
to action - without discarding the page's existing structure and voice.

- {fact_guardrail}

Output in clean Markdown with clear ## Title, ## Meta Description, then the page body.
"""


def generate_page_update(
    page_url: str,
    mode: str,
    current_title: str | None,
    current_meta: str | None,
    target_keyword: str,
    flaws: list[str] | None = None,
    revision_notes: str | None = None,
) -> str:
    model = genai.GenerativeModel(MODEL_NAME)
    template = POLISH_PROMPT if mode == "POLISH" else REWRITE_PROMPT
    revision_section = f"The project owner asked for this specific revision: \"{revision_notes}\" - prioritize addressing this.\n" if revision_notes else ""
    prompt = template.format(
        page_url=page_url,
        current_title=current_title or "(none)",
        current_meta=current_meta or "(none)",
        target_keyword=target_keyword,
        flaws=", ".join(flaws) if flaws else "none detected",
        revision_section=revision_section,
        fact_guardrail=FACT_GUARDRAIL,
    )
    response = model.generate_content(prompt)
    return response.text
