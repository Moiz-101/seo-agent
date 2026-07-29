"""Free 'authority' proxy via Open PageRank (now hosted under Keywords
Everywhere: openpagerank.keywordseverywhere.com). Free tier: 30,000 domains/
month, no card required - but needs a free Keywords Everywhere account to
generate the API key from (openpagerank.keywordseverywhere.com/dashboard).

Real "Domain Authority" is a Moz-trademarked, paid metric. Open PageRank is
a free, independent alternative (0-10 scale, based on a public backlink
graph) - it is NOT the same number Moz would report, so we always label it
as a proxy rather than presenting it as real DA.
"""
import requests

from config import OPEN_PAGERANK_API_KEY

ENDPOINT = "https://openpagerank.keywordseverywhere.com/v1/domains/bulk"


def get_authority_score(domain: str) -> dict:
    if not OPEN_PAGERANK_API_KEY:
        return {"available": False, "reason": "OPEN_PAGERANK_API_KEY not set"}

    try:
        resp = requests.post(
            ENDPOINT,
            json={"domains": [domain], "include_history": False},
            headers={"Authorization": f"Bearer {OPEN_PAGERANK_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        result = (data.get("results") or [{}])[0]
    except Exception as e:
        return {"available": False, "reason": str(e)}

    if not result.get("found"):
        return {"available": False, "reason": "no data for this domain"}

    rank_decimal = result.get("open_page_rank")
    if rank_decimal is None:
        return {"available": False, "reason": "no data for this domain"}

    return {
        "available": True,
        "authority_score_0_10": rank_decimal,
        "authority_score_0_100": round(rank_decimal * 10, 1),
        "rank": result.get("rank"),
        "referring_domains": result.get("referring_domains"),
        "source": "Open PageRank (free proxy, not Moz Domain Authority)",
    }
