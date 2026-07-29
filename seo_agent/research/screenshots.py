"""Free page screenshots via Microlink.io's keyless endpoint.

We deliberately avoid a local headless browser (Playwright/Chromium) here:
Render's free instance has 512MB RAM shared with the Telegram bot, crawler,
and Gemini calls, and a full browser process is likely to OOM or slow
everything else down. Microlink's free tier does the rendering for us.
"""
import logging

import requests

logger = logging.getLogger(__name__)

MICROLINK_ENDPOINT = "https://api.microlink.io"


def capture_screenshot(url: str) -> bytes | None:
    """Returns PNG bytes, or None if the screenshot couldn't be captured
    (best-effort - callers should keep going without it rather than fail).
    """
    try:
        resp = requests.get(
            MICROLINK_ENDPOINT,
            params={"url": url, "screenshot": "true", "meta": "false", "waitFor": 1000},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        screenshot_url = data.get("data", {}).get("screenshot", {}).get("url")
        if not screenshot_url:
            logger.warning("Microlink returned no screenshot URL for %s: %s", url, data)
            return None

        img_resp = requests.get(screenshot_url, timeout=30)
        img_resp.raise_for_status()
        return img_resp.content
    except Exception as e:
        logger.warning("Screenshot capture failed for %s: %s", url, e)
        return None
