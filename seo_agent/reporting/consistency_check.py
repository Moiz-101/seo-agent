"""Automated sanity checks run after every audit, before the site is
allowed to move on to "Scratch Start". Catches the class of bug this whole
accuracy pass was about: summary numbers that don't match the detailed
data behind them, directories mislabelled as competitors, system URLs
leaking into scoring, etc.

Returns a list of human-readable problem strings; an empty list means the
audit passed.
"""
from seo_agent.research.competitors import DIRECTORY_DOMAINS, classify_domain
from seo_agent.research.site_crawler import _is_system_url


def run_checks(site_data: dict) -> list[str]:
    issues = []

    broken = site_data.get("broken_links_report", {})
    confirmed = broken.get("confirmed_broken", [])
    internal_confirmed = [b for b in confirmed if b["type"] == "internal"]
    external_confirmed = [b for b in confirmed if b["type"] == "external"]
    unverified = broken.get("unverified", [])
    external_unverified = [b for b in unverified if b["type"] == "external"]

    if broken.get("internal_broken_occurrences") != len(internal_confirmed):
        issues.append(
            f"internal_broken_occurrences ({broken.get('internal_broken_occurrences')}) does not match "
            f"the {len(internal_confirmed)} confirmed internal rows"
        )
    if broken.get("unique_internal_broken_urls") != len({b["destination"] for b in internal_confirmed}):
        issues.append("unique_internal_broken_urls does not match the distinct destinations in confirmed_broken")
    expected_affected_pages = len({b["source_page"] for b in confirmed + unverified})
    if broken.get("affected_source_pages") != expected_affected_pages:
        issues.append(
            f"affected_source_pages ({broken.get('affected_source_pages')}) does not match the "
            f"{expected_affected_pages} distinct source pages across confirmed+unverified rows"
        )
    expected_external_unreachable = len({b["destination"] for b in external_confirmed + external_unverified})
    if broken.get("unique_external_unreachable_urls") != expected_external_unreachable:
        issues.append("unique_external_unreachable_urls does not match distinct external destinations found")

    for c in site_data.get("competitors", []):
        if c["category"] == "Direct Business Competitor" and classify_domain(c["domain"]) == "Directory/Aggregator":
            issues.append(f"{c['domain']} is a known directory/aggregator but classified as Direct Business Competitor")

    for p in site_data["crawl"]["pages"]:
        if _is_system_url(p["url"]):
            issues.append(f"system URL leaked into the crawl: {p['url']}")

    for f in site_data.get("image_alt_findings", []):
        if not f.get("image_url") or not f.get("pages"):
            issues.append("an image-alt finding is missing evidence (image URL or source page list)")
            break

    for b in confirmed:
        if not b.get("source_page") or not b.get("destination"):
            issues.append("a confirmed broken-link row is missing source/destination evidence")
            break

    return issues
