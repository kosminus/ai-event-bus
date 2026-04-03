"""Playwright tool backend — gives agents a headless browser.

Agents can navigate to pages, extract text, take screenshots, click
elements, and fill forms. The browser is managed as a singleton — started
on first use, reused across calls, closed on shutdown.

Enable in config.yaml:
    tools:
      playwright_enabled: true
      playwright_headless: true
      playwright_timeout: 30000   # ms per action

Register in main.py:
    from aiventbus.tools.playwright_backend import PlaywrightBackend
    pw = PlaywrightBackend(headless=config.tools.playwright_headless,
                           timeout=config.tools.playwright_timeout)
    executor.tool_registry.register(pw)
"""

from __future__ import annotations

import logging
from typing import Any

from aiventbus.core.tools import ToolBackend, ToolMethod

logger = logging.getLogger(__name__)


class PlaywrightBackend(ToolBackend):
    """Headless browser tool via Playwright."""

    def __init__(self, headless: bool = True, timeout: int = 30_000):
        self._headless = headless
        self._timeout = timeout
        self._playwright = None
        self._browser = None
        self._page = None

    @property
    def name(self) -> str:
        return "playwright"

    @property
    def description(self) -> str:
        return "Headless browser — navigate pages, extract text, take screenshots, click, fill forms"

    def methods(self) -> list[ToolMethod]:
        return [
            ToolMethod(
                "goto",
                "Navigate to a URL and return the page title",
                {"url": "full URL to navigate to"},
            ),
            ToolMethod(
                "get_text",
                "Extract visible text from the page or a specific element",
                {"selector": "(optional) CSS selector, omit for full page text"},
            ),
            ToolMethod(
                "screenshot",
                "Take a screenshot of the current page",
                {"path": "(optional) file path to save, defaults to /tmp/screenshot.png", "full_page": "(optional) true for full page"},
            ),
            ToolMethod(
                "click",
                "Click an element on the page",
                {"selector": "CSS selector of the element to click"},
            ),
            ToolMethod(
                "fill",
                "Fill a form field with text",
                {"selector": "CSS selector of the input", "value": "text to type"},
            ),
            ToolMethod(
                "evaluate",
                "Run JavaScript in the page and return the result",
                {"expression": "JavaScript expression to evaluate"},
            ),
            ToolMethod(
                "pdf",
                "Save the current page as PDF",
                {"path": "(optional) file path, defaults to /tmp/page.pdf"},
            ),
            ToolMethod(
                "get_links",
                "Extract all links from the page",
                {"selector": "(optional) CSS selector to scope link extraction"},
            ),
        ]

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        page = await self._ensure_page()

        if method == "goto":
            return await self._goto(page, params)
        elif method == "get_text":
            return await self._get_text(page, params)
        elif method == "screenshot":
            return await self._screenshot(page, params)
        elif method == "click":
            return await self._click(page, params)
        elif method == "fill":
            return await self._fill(page, params)
        elif method == "evaluate":
            return await self._evaluate(page, params)
        elif method == "pdf":
            return await self._pdf(page, params)
        elif method == "get_links":
            return await self._get_links(page, params)
        else:
            return {"error": f"Unknown method: {method}"}

    async def _ensure_page(self):
        """Lazy-init browser and page on first use."""
        if self._page and not self._page.is_closed():
            return self._page

        from playwright.async_api import async_playwright

        if not self._playwright:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self._headless)
            logger.info("Playwright browser started (headless=%s)", self._headless)

        context = await self._browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        self._page = await context.new_page()
        self._page.set_default_timeout(self._timeout)
        return self._page

    async def _goto(self, page, params: dict) -> dict:
        url = params.get("url", "")
        if not url:
            return {"error": "url is required"}
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
            return {
                "url": page.url,
                "title": await page.title(),
                "status": resp.status if resp else None,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _get_text(self, page, params: dict) -> dict:
        selector = params.get("selector")
        try:
            if selector:
                el = await page.query_selector(selector)
                if not el:
                    return {"error": f"No element found for selector: {selector}"}
                text = await el.inner_text()
            else:
                text = await page.inner_text("body")
            # Truncate to avoid blowing up context
            truncated = len(text) > 50_000
            return {"text": text[:50_000], "length": len(text), "truncated": truncated}
        except Exception as e:
            return {"error": str(e)}

    async def _screenshot(self, page, params: dict) -> dict:
        path = params.get("path", "/tmp/screenshot.png")
        full_page = params.get("full_page", False)
        try:
            await page.screenshot(path=path, full_page=full_page)
            return {"path": path, "saved": True}
        except Exception as e:
            return {"error": str(e)}

    async def _click(self, page, params: dict) -> dict:
        selector = params.get("selector", "")
        if not selector:
            return {"error": "selector is required"}
        try:
            await page.click(selector)
            return {"clicked": selector}
        except Exception as e:
            return {"error": str(e)}

    async def _fill(self, page, params: dict) -> dict:
        selector = params.get("selector", "")
        value = params.get("value", "")
        if not selector:
            return {"error": "selector is required"}
        try:
            await page.fill(selector, value)
            return {"filled": selector, "value": value}
        except Exception as e:
            return {"error": str(e)}

    async def _evaluate(self, page, params: dict) -> dict:
        expression = params.get("expression", "")
        if not expression:
            return {"error": "expression is required"}
        try:
            result = await page.evaluate(expression)
            return {"result": result}
        except Exception as e:
            return {"error": str(e)}

    async def _pdf(self, page, params: dict) -> dict:
        path = params.get("path", "/tmp/page.pdf")
        try:
            await page.pdf(path=path)
            return {"path": path, "saved": True}
        except Exception as e:
            return {"error": str(e)}

    async def _get_links(self, page, params: dict) -> dict:
        selector = params.get("selector", "a[href]")
        try:
            links = await page.evaluate(f"""
                () => Array.from(document.querySelectorAll('{selector}')).map(a => ({{
                    text: a.innerText.trim().substring(0, 100),
                    href: a.href
                }})).filter(l => l.href && l.href.startsWith('http')).slice(0, 100)
            """)
            return {"links": links, "count": len(links)}
        except Exception as e:
            return {"error": str(e)}

    async def close(self) -> None:
        """Shutdown browser. Call on app shutdown."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._page = None
        logger.info("Playwright browser closed")
