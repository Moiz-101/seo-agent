"""Google Search Console API wrapper (free) for rank/traffic monitoring.

Requires a Google Cloud service account with access granted to the target
property inside Search Console (Settings > Users and permissions > add the
service account email as a user).
"""
from datetime import date, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import GSC_SERVICE_ACCOUNT_FILE, GSC_SITE_URL

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


def _get_service():
    credentials = service_account.Credentials.from_service_account_file(
        GSC_SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("searchconsole", "v1", credentials=credentials)


def get_keyword_performance(days: int = 28, row_limit: int = 100) -> list[dict]:
    """Returns query-level clicks/impressions/position for the last N days."""
    service = _get_service()
    end = date.today() - timedelta(days=2)  # GSC data has ~2 day lag
    start = end - timedelta(days=days)

    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["query"],
        "rowLimit": row_limit,
    }
    response = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()

    rows = response.get("rows", [])
    return [
        {
            "query": r["keys"][0],
            "clicks": r["clicks"],
            "impressions": r["impressions"],
            "ctr": round(r["ctr"] * 100, 2),
            "position": round(r["position"], 1),
        }
        for r in rows
    ]


def get_position_for_keyword(keyword: str, days: int = 28) -> dict | None:
    results = get_keyword_performance(days=days, row_limit=1000)
    for r in results:
        if r["query"].lower() == keyword.lower():
            return r
    return None
