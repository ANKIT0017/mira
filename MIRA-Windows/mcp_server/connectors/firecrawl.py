"""
Local Firecrawl-compatible connector for MIRA.

This keeps the existing firecrawl.* tool names, but it does not require the
separate Firecrawl Docker/API service. The implementation borrows the useful
parts of the local Firecrawl codebase: browser-style fetching, ad/media request
blocking, clean markdown extraction, URL mapping, small crawls, search+scrape,
and LLM-backed structured extraction.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import requests
try:
    import html2text
except Exception:
    html2text = None
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None
try:
    from readability import Document
except Exception:
    Document = None

from mcp_server.tools import register_tool


DEFAULT_TIMEOUT = 20
MAX_HTML_CHARS = 300_000
MAX_MARKDOWN_CHARS = 80_000
SANDBOX = Path(__file__).resolve().parents[2] / "SANDBOX"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

AD_SERVING_DOMAINS = (
    "doubleclick.net",
    "adservice.google.com",
    "googlesyndication.com",
    "googletagservices.com",
    "googletagmanager.com",
    "google-analytics.com",
    "adsystem.com",
    "adservice.com",
    "adnxs.com",
    "ads-twitter.com",
    "facebook.net",
    "fbcdn.net",
    "amazon-adsystem.com",
)

MEDIA_RESOURCE_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp|ico|mp3|mp4|avi|flac|ogg|wav|webm|woff2?|ttf)(?:[?#].*)?$",
    re.IGNORECASE,
)


def _valid_url(url: str) -> bool:
    try:
        p = urlparse(str(url))
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False


def _same_site(base: str, candidate: str) -> bool:
    b = urlparse(base)
    c = urlparse(candidate)
    b_host = b.netloc.lower().removeprefix("www.")
    c_host = c.netloc.lower().removeprefix("www.")
    return b_host == c_host


def _normalize_url(base: str, href: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return ""
    joined = urljoin(base, href)
    joined = urldefrag(joined)[0]
    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return joined


def _request_html(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    skip_tls_verification: bool = False,
) -> dict[str, Any]:
    final_headers = dict(DEFAULT_HEADERS)
    if isinstance(headers, dict):
        final_headers.update({str(k): str(v) for k, v in headers.items()})
    response = requests.get(
        url,
        headers=final_headers,
        timeout=max(1, int(timeout or DEFAULT_TIMEOUT)),
        verify=not bool(skip_tls_verification),
        allow_redirects=True,
    )
    response.raise_for_status()
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return {
        "html": response.text[:MAX_HTML_CHARS],
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "final_url": response.url,
        "content_type": response.headers.get("content-type", ""),
    }


def _playwright_html(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    wait_after_load: int = 0,
    check_selector: str | None = None,
    skip_tls_verification: bool = False,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(f"playwright is not installed: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=DEFAULT_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
                ignore_https_errors=bool(skip_tls_verification),
                extra_http_headers=headers or {},
            )

            def route_handler(route, request):
                host = urlparse(request.url).hostname or ""
                if MEDIA_RESOURCE_RE.search(request.url):
                    return route.abort()
                if any(domain in host for domain in AD_SERVING_DOMAINS):
                    return route.abort()
                return route.continue_()

            context.route("**/*", route_handler)
            page = context.new_page()
            response = page.goto(url, wait_until="load", timeout=max(1, int(timeout or DEFAULT_TIMEOUT)) * 1000)
            if wait_after_load:
                page.wait_for_timeout(int(wait_after_load))
            if check_selector:
                page.wait_for_selector(check_selector, timeout=max(1, int(timeout or DEFAULT_TIMEOUT)) * 1000)
            html = page.content()
            return {
                "html": html[:MAX_HTML_CHARS],
                "status_code": response.status if response else None,
                "headers": response.all_headers() if response else {},
                "final_url": page.url,
                "content_type": (response.header_value("content-type") if response else "") or "",
            }
        finally:
            browser.close()


def _html_to_clean_markdown(html: str, url: str = "") -> dict[str, Any]:
    if BeautifulSoup is None:
        plain = re.sub(r"<[^>]+>", " ", html or "")
        plain = re.sub(r"\s+", " ", plain).strip()
        return {
            "title": "",
            "description": "",
            "markdown": plain[:MAX_MARKDOWN_CHARS],
            "text": plain[:MAX_MARKDOWN_CHARS],
            "links": [],
        }
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "canvas", "form"]):
        tag.decompose()

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    description = ""
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if desc_tag:
        description = str(desc_tag.get("content") or "").strip()

    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = _normalize_url(url, str(a.get("href") or ""))
        if not href or href in seen:
            continue
        seen.add(href)
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        links.append({"url": href, "text": text[:160]})

    content_html = ""
    try:
        if Document is None:
            raise RuntimeError("readability not installed")
        doc = Document(str(soup))
        content_html = doc.summary()
        title = doc.title() or title
    except Exception:
        body = soup.body or soup
        content_html = str(body)

    if html2text is not None:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.ignore_emphasis = False
        h.body_width = 0
        markdown = h.handle(content_html or "")
    else:
        markdown = BeautifulSoup(content_html or "", "html.parser").get_text("\n", strip=True)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    text = re.sub(r"\s+", " ", BeautifulSoup(content_html or "", "html.parser").get_text(" ", strip=True)).strip()
    return {
        "title": title,
        "description": description,
        "markdown": markdown[:MAX_MARKDOWN_CHARS],
        "text": text[:MAX_MARKDOWN_CHARS],
        "links": links,
    }


def _scrape_local(
    url: str,
    formats: list | None = None,
    headers: dict[str, str] | None = None,
    wait_after_load: int = 0,
    timeout: int = DEFAULT_TIMEOUT,
    check_selector: str | None = None,
    skip_tls_verification: bool = False,
    use_browser: bool | None = None,
) -> dict[str, Any]:
    if not _valid_url(url):
        return {"status": "error", "error": "Invalid URL.", "url": url}
    formats = formats or ["markdown"]
    try:
        fetched = None
        if use_browser:
            fetched = _playwright_html(
                url,
                headers=headers,
                timeout=timeout,
                wait_after_load=wait_after_load,
                check_selector=check_selector,
                skip_tls_verification=skip_tls_verification,
            )
        else:
            try:
                fetched = _request_html(
                    url,
                    headers=headers,
                    timeout=timeout,
                    skip_tls_verification=skip_tls_verification,
                )
            except Exception as req_error:
                # If requests fails and Playwright is installed, use the browser path.
                try:
                    fetched = _playwright_html(
                        url,
                        headers=headers,
                        timeout=timeout,
                        wait_after_load=wait_after_load,
                        check_selector=check_selector,
                        skip_tls_verification=skip_tls_verification,
                    )
                except Exception:
                    raise req_error

        html = str(fetched.get("html") or "")
        final_url = str(fetched.get("final_url") or url)
        converted = _html_to_clean_markdown(html, final_url)
        result = {
            "success": True,
            "url": final_url,
            "metadata": {
                "title": converted.get("title"),
                "description": converted.get("description"),
                "sourceURL": final_url,
                "statusCode": fetched.get("status_code"),
                "contentType": fetched.get("content_type"),
                "scraped_at": datetime.now().isoformat(),
                "engine": "local-playwright" if use_browser else "local-requests",
            },
        }
        if "markdown" in formats:
            result["markdown"] = converted.get("markdown", "")
        if "html" in formats:
            result["html"] = html
        if "text" in formats:
            result["text"] = converted.get("text", "")
        result["links"] = converted.get("links", [])
        return result
    except Exception as e:
        return {"status": "error", "success": False, "error": f"Failed to scrape {url}: {e}", "url": url}


@register_tool(
    "firecrawl.scrape",
    category="Web Scraping",
    description="Scrape a single URL locally and return clean markdown/html/links",
    example='{"tool": "firecrawl.scrape", "args": {"url": "https://example.com"}}',
    notes="Runs inside MIRA; no external Firecrawl Docker/API service required.",
)
def scrape_url(
    url: str,
    formats: list = None,
    wait_after_load: int = 0,
    timeout: int = DEFAULT_TIMEOUT,
    headers: dict = None,
    check_selector: str = None,
    skip_tls_verification: bool = False,
    use_browser: bool = False,
) -> dict:
    return _scrape_local(
        url,
        formats=formats,
        headers=headers,
        wait_after_load=wait_after_load,
        timeout=timeout,
        check_selector=check_selector,
        skip_tls_verification=skip_tls_verification,
        use_browser=use_browser,
    )


@register_tool(
    "firecrawl.map",
    category="Web Scraping",
    description="Discover URLs linked from a page locally",
    example='{"tool": "firecrawl.map", "args": {"url": "https://example.com"}}',
)
def map_website(url: str, limit: int = 200, same_domain: bool = True) -> dict:
    scraped = _scrape_local(url, formats=["markdown"], timeout=DEFAULT_TIMEOUT)
    if scraped.get("status") == "error":
        return scraped
    urls = []
    seen = set()
    for link in scraped.get("links", []) or []:
        href = str(link.get("url") or "")
        if same_domain and not _same_site(url, href):
            continue
        if href and href not in seen:
            seen.add(href)
            urls.append(href)
        if len(urls) >= int(limit or 200):
            break
    saved_to = None
    if len(urls) > 20:
        SANDBOX.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", urlparse(url).netloc + urlparse(url).path).strip("_")
        output = SANDBOX / f"map_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        output.write_text("\n".join(urls), encoding="utf-8")
        saved_to = str(output)
    return {
        "success": True,
        "url": url,
        "total_urls": len(urls),
        "urls": urls[:20],
        "saved_to": saved_to,
    }


@register_tool(
    "firecrawl.crawl",
    category="Web Scraping",
    description="Crawl a small same-domain site locally and save scraped pages to SANDBOX",
    example='{"tool": "firecrawl.crawl", "args": {"url": "https://docs.site.com", "limit": 20}}',
)
def crawl_website(url: str, limit: int = 20, max_depth: int = 2, same_domain: bool = True) -> dict:
    if not _valid_url(url):
        return {"status": "error", "error": "Invalid URL.", "url": url}
    limit = max(1, min(int(limit or 20), 100))
    max_depth = max(0, min(int(max_depth or 2), 5))
    queue = deque([(url, 0)])
    seen = set()
    pages = []
    while queue and len(pages) < limit:
        current, depth = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        if same_domain and not _same_site(url, current):
            continue
        scraped = _scrape_local(current, formats=["markdown"], timeout=DEFAULT_TIMEOUT)
        if scraped.get("status") == "error":
            pages.append({"url": current, "error": scraped.get("error", "")})
            continue
        pages.append({
            "url": current,
            "markdown": scraped.get("markdown", ""),
            "metadata": scraped.get("metadata", {}),
        })
        if depth < max_depth:
            for link in scraped.get("links", []) or []:
                href = str(link.get("url") or "")
                if href and href not in seen:
                    queue.append((href, depth + 1))
    SANDBOX.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", urlparse(url).netloc + urlparse(url).path).strip("_")
    output = SANDBOX / f"crawl_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.write_text(
        json.dumps(
            {
                "url": url,
                "crawled_at": datetime.now().isoformat(),
                "total_pages": len(pages),
                "pages": pages,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    return {
        "success": True,
        "url": url,
        "pages_crawled": len(pages),
        "saved_to": str(output),
        "filename": output.name,
        "preview": [{"url": p.get("url"), "title": (p.get("metadata") or {}).get("title")} for p in pages[:5]],
    }


@register_tool(
    "firecrawl.search",
    category="Web Scraping",
    description="Search the web and locally scrape result pages into markdown",
    example='{"tool": "firecrawl.search", "args": {"query": "deep research topic", "limit": 5}}',
)
def search_web(query: str, limit: int = 5) -> dict:
    try:
        from mcp_server.connectors.search import search_duckduckgo
    except Exception as e:
        return {"status": "error", "error": f"Search connector unavailable: {e}", "query": query}

    search = search_duckduckgo(query=query, max_results=limit)
    if isinstance(search, dict) and search.get("status") == "error":
        return search
    results = search.get("results", []) if isinstance(search, dict) else []
    enriched = []
    for result in results[: max(1, int(limit or 5))]:
        url = str(result.get("url") or "")
        scraped = _scrape_local(url, formats=["markdown"], timeout=DEFAULT_TIMEOUT) if url else {}
        enriched.append({
            "url": url,
            "title": result.get("title", ""),
            "description": result.get("snippet", ""),
            "markdown": str(scraped.get("markdown") or result.get("snippet") or "")[:5000],
            "metadata": scraped.get("metadata", {}) if isinstance(scraped, dict) else {},
            "scrape_error": scraped.get("error") if isinstance(scraped, dict) else None,
        })
        time.sleep(0.1)
    return {
        "success": True,
        "query": query,
        "total_results": len(enriched),
        "results": enriched,
    }


@register_tool(
    "firecrawl.extract",
    category="Web Scraping",
    description="Extract structured JSON from a URL locally using the configured MIRA LLM",
    example='{"tool": "firecrawl.extract", "args": {"url": "https://products.com", "schema": {"products": [{"name": "string"}]}}}',
    notes="Runs inside MIRA using the local model; no Firecrawl backend or OpenAI key required.",
)
def extract_structured(url: str, schema: dict) -> dict:
    scraped = _scrape_local(url, formats=["markdown"], timeout=DEFAULT_TIMEOUT)
    if scraped.get("status") == "error":
        return scraped
    markdown = str(scraped.get("markdown") or "")
    try:
        from agent.model_adapter import generate
    except Exception as e:
        return {"status": "error", "error": f"Model adapter unavailable: {e}", "url": url}

    prompt = (
        "Extract structured data from this page. Return JSON only matching the requested schema as closely as possible.\n"
        f"URL: {url}\n"
        f"Schema:\n{json.dumps(schema, indent=2, ensure_ascii=False)}\n\n"
        f"Page markdown:\n{markdown[:12000]}"
    )
    try:
        raw = generate(prompt, model="local", max_tokens=1800, timeout=180, temperature=0.1)
    except Exception as e:
        return {"status": "error", "error": f"Extraction LLM failed: {e}", "url": url}
    text = str(raw or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
    start = min([i for i in [text.find("{"), text.find("[")] if i >= 0], default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    extracted: Any = text
    if start >= 0 and end > start:
        try:
            extracted = json.loads(text[start:end + 1])
        except Exception:
            extracted = text
    return {
        "success": True,
        "url": url,
        "extracted_data": extracted,
        "schema_used": schema,
        "metadata": scraped.get("metadata", {}),
    }


print("[FIRECRAWL] Local connector loaded - no Docker/API service required")
