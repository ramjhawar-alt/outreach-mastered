"""Playwright-based browser module for loading web pages."""

import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from .config import PAGE_LOAD_TIMEOUT_MS, REQUEST_DELAY_SEC

# Scroll settings for infinite-scroll pages
SCROLL_PAUSE_SEC = 2
SCROLL_ATTEMPTS = 25


def load_page(
    url: str,
    scroll_to_load: bool = False,
    wait_until: str | None = None,
) -> tuple[str, str] | None:
    """
    Load a webpage and return its HTML content and visible text.

    Args:
        url: The URL to load (must include scheme, e.g. https://)
        scroll_to_load: If True, scroll down to trigger lazy/infinite scroll loading

    Returns:
        Tuple of (html_content, visible_text) or None if loading failed.
    """
    if not url.strip():
        return None

    # Ensure URL has a scheme
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_LOAD_TIMEOUT_MS)

        try:
            effective_wait = wait_until or (
                "networkidle" if scroll_to_load else "domcontentloaded"
            )
            page.goto(url, wait_until=effective_wait)
            time.sleep(REQUEST_DELAY_SEC)  # Be respectful to servers

            if scroll_to_load:
                prev_count = 0
                for i in range(SCROLL_ATTEMPTS):
                    # Scroll to bottom
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(SCROLL_PAUSE_SEC)
                    # Incremental scroll (triggers lazy load on some sites)
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                    time.sleep(SCROLL_PAUSE_SEC)
                    count = page.evaluate(
                        """() => document.querySelectorAll('a[href*="/companies/"]').length"""
                    )
                    if count == prev_count and count > 0 and i >= 3:
                        break
                    prev_count = count

            html = page.content()
            visible_text = page.evaluate(
                """() => {
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT,
                    null,
                    false
                );
                let text = '';
                let node;
                while (node = walker.nextNode()) {
                    const parent = node.parentElement;
                    if (parent && (
                        parent.tagName === 'SCRIPT' ||
                        parent.tagName === 'STYLE' ||
                        parent.tagName === 'NOSCRIPT'
                    )) continue;
                    text += node.textContent + ' ';
                }
                return text.replace(/\\s+/g, ' ').trim();
            }"""
            )
            return (html, visible_text or "")
        except Exception:
            return None
        finally:
            browser.close()
