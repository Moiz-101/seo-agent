"""Free page screenshots via Microlink.io's keyless endpoint.

We deliberately avoid a local headless browser (Playwright/Chromium) here:
Render's free instance has 512MB RAM shared with the Telegram bot, crawler,
and Gemini calls, and a full browser process is likely to OOM or slow
everything else down. Microlink's free tier does the rendering for us.
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

MICROLINK_ENDPOINT = "https://api.microlink.io"


def capture_screenshot(url: str, attempts: int = 3) -> bytes | None:
    """Returns PNG bytes, or None if the screenshot couldn't be captured
    (best-effort - callers should keep going without it rather than fail).

    Microlink's CDN occasionally serves a tiny placeholder if the image is
    fetched right as it's generated, before it's fully propagated - the
    downloaded size won't match the size Microlink itself reported, so we
    retry a couple of times in that case.
    """
    content = None
    for attempt in range(attempts):
        try:
            params = {"url": url, "screenshot": "true", "meta": "false", "waitFor": 1000}
            if attempt > 0:
                params["force"] = "true"  # bypass Microlink's cache of a bad/incomplete render

            resp = requests.get(MICROLINK_ENDPOINT, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            screenshot = data.get("data", {}).get("screenshot", {})
            screenshot_url = screenshot.get("url")
            expected_size = screenshot.get("size")
            if not screenshot_url:
                logger.warning("Microlink returned no screenshot URL for %s: %s", url, data)
                continue

            img_resp = requests.get(screenshot_url, timeout=30)
            img_resp.raise_for_status()
            content = img_resp.content
            if not expected_size or len(content) >= expected_size * 0.9:
                return content
            logger.warning(
                "Screenshot for %s came back too small (%d/%d bytes), retrying with force...", url, len(content), expected_size
            )
        except Exception as e:
            logger.warning("Screenshot capture attempt %d failed for %s: %s", attempt + 1, url, e)
        time.sleep(1.5 * (attempt + 1))

    return content  # last attempt's content (possibly still small), better than nothing
