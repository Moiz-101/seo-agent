"""Free 'authority' proxy via Open PageRank (openpagerank.com).

Real "Domain Authority" is a Moz-trademarked, paid metric. Open PageRank is
a free, independent alternative (0-10 scale, based on a public backlink
graph) - it is NOT the same number Moz would report, so we always label it
as a proxy rather than presenting it as real DA.
"""
import requests

from config import OPEN_PAGERANK_API_KEY

ENDPOINT = "https://openpagerank.com/api/v1.0/getPageRank"


def get_authority_score(domain: str) -> dict:
    if not OPEN_PAGERANK_API_KEY:
        return {"available": False, "reason": "OPEN_PAGERANK_API_KEY not set"}

    try:
        resp = requests.get(
            ENDPOINT,
            params={"domains[]": domain},
            headers={"API-OPR": OPEN_PAGERANK_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        result = (data.get("response") or [{}])[0]
    except Exception as e:
        return {"available": False, "reason": str(e)}

    rank_decimal = result.get("page_rank_decimal")
    if rank_decimal is None:
        return {"available": False, "reason": result.get("error", "no data for this domain")}

    return {
        "available": True,
        "authority_score_0_10": rank_decimal,
        "authority_score_0_100": round(rank_decimal * 10, 1),
        "rank": result.get("rank"),
        "source": "Open PageRank (free proxy, not Moz Domain Authority)",
    }
