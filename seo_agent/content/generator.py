"""Content generation via Google Gemini (free tier)."""
import google.generativeai as genai

from config import GEMINI_API_KEY

genai.configure(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-1.5-flash"  # free-tier friendly; use gemini-1.5-pro for higher quality if quota allows

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
