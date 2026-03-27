"""
Substack Scraper API
====================
FastAPI + Crawl4AI + Featherless AI (OpenAI-compatible)

Scrapes Substack newsletters, dismisses subscription popups,
extracts content, and optionally summarizes via LLM.

Auto-detects page type (homepage vs article) and uses the
correct CSS selectors — no manual configuration needed.
"""

import os
import re
import time
import logging
from typing import Optional
from enum import Enum
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from openai import OpenAI
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

load_dotenv()

# ──────────────────────────────────────────────
# Logging — verbose by default for transparency
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("substack-scraper")

# ──────────────────────────────────────────────
# Featherless AI client (OpenAI-compatible)
# ──────────────────────────────────────────────
FEATHERLESS_BASE_URL = os.environ["FEATHERLESS_BASE_URL"]
FEATHERLESS_API_KEY = os.environ["FEATHERLESS_API_KEY"]
FEATHERLESS_MODEL = os.environ["FEATHERLESS_MODEL"]

llm_client = OpenAI(
    base_url=FEATHERLESS_BASE_URL,
    api_key=FEATHERLESS_API_KEY,
)

# ──────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────
app = FastAPI(
    title="Substack Scraper API",
    description="Crawl Substack newsletters and extract/summarize content with LLM",
    version="0.2.0",
)

# ──────────────────────────────────────────────
# Substack page type detection & selectors
# ──────────────────────────────────────────────
class SubstackPageType(str, Enum):
    ARTICLE = "article"       # /p/some-slug
    HOMEPAGE = "homepage"     # root or /archive
    UNKNOWN = "unknown"


# Substack DOM is consistent:
#   Homepage  → .portable-archive-list has the post listing
#   Article   → .available-content wraps everything, .body.markup is the post body
#   Both      → subscription popup appears on first visit
SUBSTACK_SELECTORS = {
    SubstackPageType.ARTICLE: ".available-content",
    SubstackPageType.HOMEPAGE: ".portable-archive-list",
    SubstackPageType.UNKNOWN: None,  # full page fallback
}


def _detect_page_type(url: str) -> SubstackPageType:
    """Detect Substack page type from URL pattern."""
    path = urlparse(url).path.rstrip("/")

    if "/p/" in path:
        page_type = SubstackPageType.ARTICLE
    elif path in ("", "/", "/archive"):
        page_type = SubstackPageType.HOMEPAGE
    else:
        page_type = SubstackPageType.UNKNOWN

    log.info(f"Page type: {page_type.value} (path={path})")
    return page_type


# ──────────────────────────────────────────────
# JS snippet to dismiss Substack subscription popup
# ──────────────────────────────────────────────
DISMISS_SUBSTACK_POPUP_JS = """
(async () => {
    await new Promise(r => setTimeout(r, 2000));

    // Strategy 1: Click close buttons
    const closeSelectors = [
        '.modal-close',
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        '.close-button',
        '[class*="CloseButton"]',
        '[class*="close-btn"]',
        '.dismiss-button',
        'button.button.dismiss',
        '.dialog-component .close',
        '.overlay-close',
        'button[class*="dismiss"]',
        'button[class*="Dismiss"]',
    ];
    for (const sel of closeSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            console.log(`[scraper] Popup dismissed via: ${sel}`);
            el.click();
            await new Promise(r => setTimeout(r, 500));
            break;
        }
    }

    // Strategy 2: Remove overlay/modal elements
    const overlaySelectors = [
        '.subscription-widget-wrap',
        '[class*="subscribe-prompt"]',
        '[class*="SubscribePrompt"]',
        '.modal-overlay',
        '.overlay',
        '[class*="paywall"]',
        '[class*="Paywall"]',
        '[role="dialog"]',
    ];
    for (const sel of overlaySelectors) {
        document.querySelectorAll(sel).forEach(el => {
            console.log(`[scraper] Removing overlay: ${sel}`);
            el.remove();
        });
    }

    // Strategy 3: Restore scrolling
    document.body.style.overflow = 'auto';
    document.documentElement.style.overflow = 'auto';
    console.log('[scraper] Popup dismissal complete');
})();
"""


# ──────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────
class ScrapeResult(BaseModel):
    url: str
    success: bool
    page_type: str = "unknown"
    status_code: Optional[int] = None
    title: Optional[str] = None
    markdown: Optional[str] = None
    links: Optional[dict] = None
    media: Optional[dict] = None
    error: Optional[str] = None
    crawl_time_seconds: float = 0.0


class ScrapeAndSummarizeResult(ScrapeResult):
    summary: Optional[str] = None
    llm_model: Optional[str] = None
    llm_time_seconds: float = 0.0


class ArticleResult(BaseModel):
    url: str
    title: Optional[str] = None
    markdown: Optional[str] = None
    summary: Optional[str] = None
    success: bool = True
    error: Optional[str] = None
    crawl_time_seconds: float = 0.0
    llm_time_seconds: float = 0.0


class ScrapeAllResult(BaseModel):
    newsletter_url: str
    total_articles_found: int
    articles_scraped: int
    articles: list[ArticleResult]
    llm_model: str
    total_time_seconds: float = 0.0


class HealthResponse(BaseModel):
    status: str
    version: str


# ──────────────────────────────────────────────
# Core scrape logic
# ──────────────────────────────────────────────
async def _do_scrape(url: str) -> ScrapeResult:
    log.info("=" * 60)
    log.info(f"SCRAPE: {url}")
    log.info("=" * 60)

    page_type = _detect_page_type(url)
    css_selector = SUBSTACK_SELECTORS[page_type]

    log.info(f"Auto-selected CSS: {css_selector or '(full page)'}")

    t0 = time.time()

    browser_cfg = BrowserConfig(
        browser_type="chromium",
        headless=True,
        verbose=True,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        enable_stealth=True,
    )

    crawl_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        magic=True,
        scan_full_page=True,
        js_code=DISMISS_SUBSTACK_POPUP_JS,
        wait_for="css:body",
        delay_before_return_html=5,
        page_timeout=60000,
        word_count_threshold=5,
    )
    if css_selector:
        crawl_cfg.css_selector = css_selector

    log.debug(f"Browser: headless={browser_cfg.headless}, stealth={browser_cfg.enable_stealth}")
    log.debug(f"Crawl: magic={crawl_cfg.magic}, selector={css_selector}, timeout={crawl_cfg.page_timeout}ms")

    try:
        log.info("Launching browser...")
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            log.info(f"Crawling: {url}")
            result = await crawler.arun(url, config=crawl_cfg)

        elapsed = time.time() - t0
        log.info(f"Crawl done in {elapsed:.2f}s — success={result.success}, status={result.status_code}")

        if not result.success:
            log.error(f"Crawl failed: {result.error_message}")
            return ScrapeResult(
                url=url,
                success=False,
                page_type=page_type.value,
                status_code=result.status_code,
                error=result.error_message,
                crawl_time_seconds=elapsed,
            )

        markdown_content = result.markdown.raw_markdown if result.markdown else None
        title = None
        if result.html:
            m = re.search(r"<title>(.*?)</title>", result.html, re.IGNORECASE | re.DOTALL)
            title = m.group(1).strip() if m else None

        log.info(f"Title: {title}")
        log.info(f"Markdown: {len(markdown_content) if markdown_content else 0} chars")
        log.debug(
            f"Links: {len(result.links.get('internal', [])) if result.links else 0} internal, "
            f"{len(result.links.get('external', [])) if result.links else 0} external"
        )

        return ScrapeResult(
            url=result.url,
            success=True,
            page_type=page_type.value,
            status_code=result.status_code,
            title=title,
            markdown=markdown_content,
            links=result.links,
            media=result.media,
            crawl_time_seconds=elapsed,
        )

    except Exception as e:
        elapsed = time.time() - t0
        log.exception(f"Scrape error after {elapsed:.2f}s")
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────
@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", version="0.2.0")


@app.get("/scrape", response_model=ScrapeResult)
async def scrape(
    url: str = Query(..., description="Substack URL (homepage or article)"),
):
    """
    Scrape a Substack URL and return content as markdown.
    Auto-detects page type (homepage vs article) and uses the right selectors.
    Handles subscription popups automatically.
    """
    return await _do_scrape(url=url)


@app.get("/scrape-and-summarize", response_model=ScrapeAndSummarizeResult)
async def scrape_and_summarize(
    url: str = Query(..., description="Substack URL to scrape and summarize"),
    prompt: str = Query(
        "Summarize the following newsletter content. Highlight key topics, tools, and links mentioned.",
        description="Custom prompt for the LLM",
    ),
    max_tokens: int = Query(1024, ge=64, le=4096, description="Max tokens for LLM response"),
):
    """
    Scrape a Substack URL, then summarize with Featherless AI (Mistral-7B).
    """
    log.info("=" * 60)
    log.info(f"SCRAPE + SUMMARIZE: {url}")
    log.info("=" * 60)

    # Step 1: Scrape
    scrape_result = await _do_scrape(url=url)

    if not scrape_result.success or not scrape_result.markdown:
        return ScrapeAndSummarizeResult(
            **scrape_result.model_dump(),
            summary=None,
            llm_model=FEATHERLESS_MODEL,
            llm_time_seconds=0.0,
        )

    # Step 2: Summarize with Featherless AI
    content = scrape_result.markdown
    # Qwen3-8B has 32k context; ~4 chars/token, keep room for prompt + response
    max_content_chars = 24000
    if len(content) > max_content_chars:
        log.warning(f"Content truncated: {len(content)} -> {max_content_chars} chars")
        content = content[:max_content_chars] + "\n\n[... truncated ...]"

    log.info(f"LLM input: {len(content)} chars -> {FEATHERLESS_MODEL}")

    t_llm = time.time()
    try:
        completion = llm_client.chat.completions.create(
            model=FEATHERLESS_MODEL,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that summarizes web content clearly and concisely.",
                },
                {
                    "role": "user",
                    "content": f"{prompt}\n\n---\n\n{content}",
                },
            ],
        )
        summary = completion.choices[0].message.content
        llm_elapsed = time.time() - t_llm

        log.info(f"LLM response: {len(summary)} chars in {llm_elapsed:.2f}s")
        log.debug(f"LLM usage: {completion.usage}")

        return ScrapeAndSummarizeResult(
            **scrape_result.model_dump(),
            summary=summary,
            llm_model=FEATHERLESS_MODEL,
            llm_time_seconds=llm_elapsed,
        )

    except Exception as e:
        llm_elapsed = time.time() - t_llm
        log.exception(f"LLM failed after {llm_elapsed:.2f}s")
        scrape_data = scrape_result.model_dump()
        scrape_data["error"] = f"LLM error: {str(e)}"
        return ScrapeAndSummarizeResult(
            **scrape_data,
            summary=None,
            llm_model=FEATHERLESS_MODEL,
            llm_time_seconds=llm_elapsed,
        )


@app.get("/extract-links", response_model=dict)
async def extract_links(
    url: str = Query(..., description="Substack homepage URL"),
):
    """
    Scrape a Substack homepage and return all article links.
    """
    log.info(f"EXTRACT LINKS: {url}")

    result = await _do_scrape(url=url)
    if not result.success or not result.links:
        raise HTTPException(status_code=500, detail=result.error or "Failed to extract links")

    internal = result.links.get("internal", [])
    external = result.links.get("external", [])

    # Substack articles always live at /p/slug
    article_links = [
        link for link in internal
        if "/p/" in link.get("href", "")
    ]

    log.info(f"Links: {len(article_links)} articles, {len(internal)} internal, {len(external)} external")

    return {
        "url": url,
        "article_links": article_links,
        "all_internal_links": internal,
        "all_external_links": external,
    }


# ──────────────────────────────────────────────
# Batch: scrape all articles from a newsletter
# ──────────────────────────────────────────────
def _build_browser_config() -> BrowserConfig:
    return BrowserConfig(
        browser_type="chromium",
        headless=True,
        verbose=True,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        enable_stealth=True,
    )


def _build_crawl_config(css_selector: Optional[str] = None) -> CrawlerRunConfig:
    cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        magic=True,
        scan_full_page=True,
        js_code=DISMISS_SUBSTACK_POPUP_JS,
        wait_for="css:body",
        delay_before_return_html=5,
        page_timeout=60000,
        word_count_threshold=5,
    )
    if css_selector:
        cfg.css_selector = css_selector
    return cfg


def _summarize(content: str, prompt: str, max_tokens: int) -> tuple[str, float]:
    """Summarize content via Featherless AI. Returns (summary, elapsed_seconds)."""
    max_content_chars = 24000
    if len(content) > max_content_chars:
        log.warning(f"Content truncated: {len(content)} -> {max_content_chars} chars")
        content = content[:max_content_chars] + "\n\n[... truncated ...]"

    t = time.time()
    completion = llm_client.chat.completions.create(
        model=FEATHERLESS_MODEL,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant that summarizes web content clearly and concisely.",
            },
            {
                "role": "user",
                "content": f"{prompt}\n\n---\n\n{content}",
            },
        ],
    )
    summary = completion.choices[0].message.content
    elapsed = time.time() - t
    log.info(f"LLM response: {len(summary)} chars in {elapsed:.2f}s")
    return summary, elapsed


@app.get("/scrape-all", response_model=ScrapeAllResult)
async def scrape_all(
    newsletter_url: str = Query(
        ...,
        description="Substack newsletter base URL (e.g. https://thisweekinaiengineering.com)",
    ),
    limit: int = Query(5, ge=1, le=100, description="Number of articles to scrape (newest first)"),
    summarize: bool = Query(True, description="Summarize each article with LLM"),
    prompt: str = Query(
        "Summarize this newsletter article. Highlight key topics, tools, and takeaways.",
        description="Custom LLM prompt for each article summary",
    ),
    max_tokens: int = Query(1024, ge=64, le=4096, description="Max tokens per article summary"),
):
    """
    Scrape all (or N most recent) articles from a Substack newsletter.

    1. Hits /archive?sort=new and scrolls to discover all article URLs
    2. Scrapes each article (newest first, up to `limit`)
    3. Optionally summarizes each article individually via LLM

    Each article is summarized separately so context length is never an issue.
    """
    t_total = time.time()

    # Normalize base URL
    parsed = urlparse(newsletter_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    archive_url = f"{base}/archive?sort=new"

    log.info("=" * 60)
    log.info(f"SCRAPE ALL: {base} (limit={limit}, summarize={summarize})")
    log.info("=" * 60)

    browser_cfg = _build_browser_config()

    # ── Step 1: Discover article URLs from the archive page ──
    log.info(f"Step 1/2: Discovering articles from {archive_url}")
    archive_cfg = _build_crawl_config(css_selector=".portable-archive-list")

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        archive_result = await crawler.arun(archive_url, config=archive_cfg)

    if not archive_result.success:
        raise HTTPException(status_code=500, detail=f"Failed to load archive: {archive_result.error_message}")

    internal_links = archive_result.links.get("internal", []) if archive_result.links else []
    # Deduplicate, keep order (newest first), and capture link text as title
    seen = set()
    article_urls = []
    archive_titles: dict[str, str] = {}  # url -> title from archive link text
    for link in internal_links:
        href = link.get("href", "")
        text = (link.get("text") or "").strip()
        if "/p/" in href and href not in seen:
            seen.add(href)
            article_urls.append(href)
            if text and len(text) > 5:  # skip tiny labels like "1" or icons
                archive_titles[href] = text

    total_found = len(article_urls)
    article_urls = article_urls[:limit]

    log.info(f"Found {total_found} articles, will scrape {len(article_urls)}")

    # ── Step 2: Scrape each article (reuse one browser session) ──
    log.info(f"Step 2/2: Scraping {len(article_urls)} articles...")
    article_cfg = _build_crawl_config(css_selector=".available-content")

    articles: list[ArticleResult] = []

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for i, url in enumerate(article_urls):
            log.info(f"  [{i+1}/{len(article_urls)}] {url}")
            t_article = time.time()

            try:
                result = await crawler.arun(url, config=article_cfg)
                crawl_elapsed = time.time() - t_article

                if not result.success:
                    log.error(f"  Failed: {result.error_message}")
                    articles.append(ArticleResult(
                        url=url, success=False, error=result.error_message,
                        crawl_time_seconds=crawl_elapsed,
                    ))
                    continue

                md = result.markdown.raw_markdown if result.markdown else ""
                # Title: HTML <title> → markdown heading → archive link text
                title = None
                if result.html:
                    m = re.search(r"<title>(.*?)</title>", result.html, re.IGNORECASE | re.DOTALL)
                    title = m.group(1).strip() if m else None
                if not title and md:
                    m = re.search(r"^#\s+(.+)", md, re.MULTILINE)
                    title = m.group(1).strip() if m else None
                if not title:
                    title = archive_titles.get(url)

                log.info(f"  Scraped: {title or '(no title)'} ({len(md)} chars, {crawl_elapsed:.1f}s)")

                # Summarize if requested
                summary = None
                llm_elapsed = 0.0
                if summarize and md:
                    try:
                        summary, llm_elapsed = _summarize(md, prompt, max_tokens)
                    except Exception as e:
                        log.error(f"  LLM failed for {url}: {e}")
                        summary = None

                articles.append(ArticleResult(
                    url=url,
                    title=title,
                    markdown=md,
                    summary=summary,
                    success=True,
                    crawl_time_seconds=crawl_elapsed,
                    llm_time_seconds=llm_elapsed,
                ))

            except Exception as e:
                crawl_elapsed = time.time() - t_article
                log.exception(f"  Error scraping {url}")
                articles.append(ArticleResult(
                    url=url, success=False, error=str(e),
                    crawl_time_seconds=crawl_elapsed,
                ))

    total_elapsed = time.time() - t_total
    succeeded = sum(1 for a in articles if a.success)
    log.info(f"Done: {succeeded}/{len(article_urls)} articles in {total_elapsed:.1f}s")

    return ScrapeAllResult(
        newsletter_url=base,
        total_articles_found=total_found,
        articles_scraped=succeeded,
        articles=articles,
        llm_model=FEATHERLESS_MODEL if summarize else "none",
        total_time_seconds=total_elapsed,
    )


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    log.info("Starting Substack Scraper API on http://localhost:8000")
    log.info("Docs at http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")
