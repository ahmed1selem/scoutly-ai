from botasaurus.request import request
from botasaurus.browser import browser
import json
import logging
import os
import time
from pathlib import Path
from bs4 import BeautifulSoup

# Bright Data SDK logs zone discovery on every client open (very noisy).
logging.getLogger("brightdata.core.zone_manager").setLevel(logging.WARNING)


# ── Bright Data Web Unlocker (optional proxy bypass) ─────────────────────────
# If BRIGHTDATA_API_KEY is set in the environment (or a .env file at the
# project root), every botasaurus_browser_get_json call is routed through
# Bright Data's Web Unlocker instead of opening a local Chrome instance.
# This means SofaScore never sees your real IP, so rate-limit blocks don't
# happen.  All other callers (FBref, Understat) are unaffected.

def _load_env_file() -> None:
    """Load .env from the project root (4 levels up from this file) if present."""
    try:
        from dotenv import load_dotenv
        root = Path(__file__).resolve().parent.parent.parent.parent
        env_path = root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except ImportError:
        pass  # python-dotenv not installed — rely on shell env vars

_load_env_file()

# Cache the key once at import time so we don't hit os.getenv on every call
_BRIGHTDATA_KEY: str | None = os.getenv("BRIGHTDATA_API_KEY")


def _brightdata_get_json(url: str) -> dict:
    """
    Fetch a JSON URL via Bright Data's Web Unlocker.

    Bright Data routes the request through a pool of residential IPs that
    rotate automatically, so SofaScore sees a different IP on each call.
    result.data is the raw response body — for SofaScore's JSON API endpoints
    that's always a JSON string which we parse ourselves.
    """
    import certifi
    from brightdata import SyncBrightDataClient

    # Pass certifi's CA bundle explicitly — required on macOS python.org installs
    # where Python doesn't have access to system root certificates by default.
    with SyncBrightDataClient(
        token=_BRIGHTDATA_KEY,
        ssl_ca_cert=certifi.where(),
    ) as client:
        result = client.scrape_url(url, response_format="raw")

    if result.status != "ready":
        # Non-ready statuses (error / timeout / in_progress) treated as empty
        # so _ss_retry can handle them with backoff.
        return {}

    data = result.data
    if not data:
        return {}

    # result.data may already be a dict if the SDK auto-parsed JSON
    if isinstance(data, dict):
        return data

    text = str(data).strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ==================================================================================================
def botasaurus_request_get_json(url: str, delay: int = 0) -> dict:
    """Use Botasaurus REQUESTS module to get JSON from page.

    :param url: The URL to request
    :type url: str
    :param delay: Seconds to wait after the request (default: 0)
    :type delay: int
    :raises TypeError: If any of the parameters are the wrong type
    :raises ValueError: If ``delay`` is negative
    :return: JSON data
    :rtype: dict
    """
    if not isinstance(url, str):
        raise TypeError("`url` must be a string.")
    if not isinstance(delay, int):
        raise TypeError("`delay` must be an int.")
    if delay < 0:
        raise ValueError("`delay` must be non-negative.")

    @request(output=None, create_error_logs=False)
    def _get_json(request, url):  # type: ignore
        response = request.get(url)
        if delay > 0:
            time.sleep(delay)
        return response.json()

    return _get_json(url)

# ==================================================================================================
def botasaurus_browser_get_json(
        url: str, headless: bool = True, block_images_and_css: bool = True,
        wait_for_complete_page_load: bool = True, delay: int = 0
) -> dict:
    """Use Botasaurus BROWSER module to get JSON from page.

    If the BRIGHTDATA_API_KEY environment variable is set, the request is
    routed through Bright Data's Web Unlocker instead of opening a local
    Chrome instance.  This provides automatic IP rotation so SofaScore
    rate-limits are avoided entirely.

    :param url: The URL to scrape
    :type url: str
    :param headless: Whether to run the browser in headless mode (ignored when Bright Data is active)
    :type headless: bool
    :param block_images_and_css: Whether to block images and CSS (ignored when Bright Data is active)
    :type block_images_and_css: bool
    :param wait_for_complete_page_load: Whether to wait for the page to load completely (ignored when Bright Data is active)
    :type wait_for_complete_page_load: bool
    :param delay: Seconds to wait after the request (default: 0)
    :type delay: int
    :raises TypeError: If any of the parameters are the wrong type
    :raises ValueError: If ``delay`` is negative
    :return: JSON data
    :rtype: dict
    """
    if not isinstance(url, str):
        raise TypeError("`url` must be a string.")
    if not isinstance(headless, bool):
        raise TypeError("`headless` must be a bool.")
    if not isinstance(block_images_and_css, bool):
        raise TypeError("`block_images_and_css` must be a bool.")
    if not isinstance(wait_for_complete_page_load, bool):
        raise TypeError("`wait_for_complete_page_load` must be a bool.")
    if not isinstance(delay, int):
        raise TypeError("`delay` must be an int.")
    if delay < 0:
        raise ValueError("`delay` must be non-negative.")

    # ── Bright Data path ──────────────────────────────────────────────────────
    if _BRIGHTDATA_KEY:
        result = _brightdata_get_json(url)
        if delay > 0:
            time.sleep(delay)
        return result

    # ── Local Chrome path (fallback when no API key is configured) ────────────
    @browser(
        headless=headless, block_images_and_css=block_images_and_css,
        wait_for_complete_page_load=wait_for_complete_page_load,
        output=None, create_error_logs=False
    )
    def _get_json(driver, url):  # type: ignore
        driver.get(url)
        if delay > 0:
            time.sleep(delay)
        text = (driver.page_text or "").strip()
        # Return an empty dict when the page is blank (rate-limit / bot-detection
        # response from SofaScore).  This avoids JSONDecodeError propagating into
        # botasaurus's @browser error handler, which would block on input().
        if not text:
            return {}
        return json.loads(text)

    return _get_json(url)

# ==================================================================================================
def botasaurus_request_get_soup(url: str, delay: int = 0) -> BeautifulSoup:
    """Use Botasaurus REQUESTS module to get Soup from page.

    :param url: The URL to request
    :type url: str
    :param delay: Seconds to wait after the request (default: 0)
    :type delay: int
    :raises TypeError: If any of the parameters are the wrong type
    :raises ValueError: If ``delay`` is negative
    :return: BeautifulSoup object
    :rtype: BeautifulSoup
    """
    if not isinstance(url, str):
        raise TypeError("`url` must be a string.")
    if not isinstance(delay, int):
        raise TypeError("`delay` must be an int.")
    if delay < 0:
        raise ValueError("`delay` must be non-negative.")

    @request(output=None, create_error_logs=False)
    def _get_soup(request, url):  # type: ignore
        response = request.get(url)
        if delay > 0:
            time.sleep(delay)
        soup = BeautifulSoup(response.content, "html.parser")
        return soup

    return _get_soup(url)

# ==================================================================================================
def botasaurus_browser_get_soup(
        url: str, headless: bool = False, block_images_and_css: bool = False,
        wait_for_complete_page_load: bool = True, delay: int = 0
) -> BeautifulSoup:
    """ Use Botasaurus BROWSER module to get Soup from page.

    :param url: The URL to scrape
    :type url: str
    :param headless: Whether to run the browser in headless mode
    :type headless: bool
    :param block_images_and_css: Whether to block images and CSS
    :type block_images_and_css: bool
    :param wait_for_complete_page_load: Whether to wait for the page to load completely
    :type wait_for_complete_page_load: bool
    :param delay: Seconds to wait after the request (default: 0)
    :type delay: int
    :raises TypeError: If any of the parameters are the wrong type
    :raises ValueError: If ``delay`` is negative
    :return: BeautifulSoup object
    :rtype: BeautifulSoup
    """
    if not isinstance(url, str):
        raise TypeError("`url` must be a string.")
    if not isinstance(headless, bool):
        raise TypeError("`headless` must be a bool.")
    if not isinstance(block_images_and_css, bool):
        raise TypeError("`block_images_and_css` must be a bool.")
    if not isinstance(wait_for_complete_page_load, bool):
        raise TypeError("`wait_for_complete_page_load` must be a bool.")
    if not isinstance(delay, int):
        raise TypeError("`delay` must be an int.")
    if delay < 0:
        raise ValueError("`delay` must be non-negative.")

    @browser(
        headless=headless, block_images_and_css=block_images_and_css,
        wait_for_complete_page_load=wait_for_complete_page_load,
        output=None, create_error_logs=False
    )
    def _get_soup(driver, url):  # type: ignore
        driver.get(url)
        if delay > 0:
            time.sleep(delay)
        return BeautifulSoup(driver.page_html, "html.parser")

    return _get_soup(url)
