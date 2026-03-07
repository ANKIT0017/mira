"""
Firecrawl Connector for MIRA
Provides advanced web scraping, crawling, and extraction tools using self-hosted Firecrawl
"""

import sys
import os
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent.parent))
from mcp_server.tools import register_tool
import json
from datetime import datetime

# Initialize Firecrawl client (lazy loading to avoid import errors if not installed)
_firecrawl_client = None

def get_firecrawl_client():
    """Lazy initialization of Firecrawl client"""
    global _firecrawl_client
    if _firecrawl_client is None:
        try:
            from firecrawl import Firecrawl
            # Read API URL from environment or use default (port 3100 to avoid Windows port conflicts)
            api_url = os.getenv("FIRECRAWL_API_URL", "http://localhost:3100")
            _firecrawl_client = Firecrawl(api_url=api_url)
            print(f"[FIRECRAWL] Connected to self-hosted instance at {api_url}")
        except ImportError:
            raise ImportError(
                "firecrawl-py is not installed. Run: pip install firecrawl-py"
            )
        except Exception as e:
            api_url = os.getenv("FIRECRAWL_API_URL", "http://localhost:3100")
            raise ConnectionError(
                f"Failed to connect to Firecrawl at {api_url}. "
                f"Ensure Firecrawl Docker containers are running. Error: {e}"
            )
    return _firecrawl_client


@register_tool(
    "firecrawl.scrape",
    category="Web Scraping",
    description="Scrape a single URL → Returns full markdown content",
    example='{"tool": "firecrawl.scrape", "args": {"url": "https://example.com"}}',
    notes="⚠️ Returns full content in 'markdown' field. DO NOT call web.summarize after this!"
)
def scrape_url(url: str, formats: list = None):
    """
    Scrape a single URL with Firecrawl
    
    Args:
        url: URL to scrape
        formats: List of output formats (markdown, html, screenshot)
    
    Returns:
        dict: Scraped content with markdown, url, and metadata
    """
    if formats is None:
        formats = ["markdown"]
    
    try:
        print(f"[FIRECRAWL] Scraping {url} with formats: {formats}")
        client = get_firecrawl_client()
        print(f"[FIRECRAWL] Client obtained, calling scrape()...")
        
        # Anti-bot options (passed as keyword args to ScrapeOptions)
        doc = client.scrape(
            url,
            formats=formats,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            skip_tls_verification=True,
            timeout=60000
        )
        print(f"[FIRECRAWL] Scrape complete, doc type: {type(doc)}")
        
        # doc is a Document object with attributes: markdown, html, links, metadata, etc.
        result = {
            "success": True,
            "url": url,  # Original URL (doc doesn't have .url attribute)
            "markdown": doc.markdown if hasattr(doc, 'markdown') and doc.markdown else None,
            "html": doc.html if hasattr(doc, 'html') and doc.html else None,
            "metadata": {
                "title": doc.metadata.title if doc.metadata and hasattr(doc.metadata, 'title') else None,
                "description": doc.metadata.description if doc.metadata and hasattr(doc.metadata, 'description') else None,
                "language": doc.metadata.language if doc.metadata and hasattr(doc.metadata, 'language') else None,
                "scraped_at": datetime.now().isoformat()
            }
        }
        print(f"[FIRECRAWL] Success! Got {len(result.get('markdown', '') or '')} chars of markdown")
        return result
    except Exception as e:
        import traceback
        print(f"[FIRECRAWL] ERROR in scrape_url: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "error": f"Failed to scrape {url}: {str(e)}",
            "url": url
        }


@register_tool(
    "firecrawl.crawl",
    category="Web Scraping",
    description="Crawl entire website recursively → Saves JSON file with all pages",
    example='{"tool": "firecrawl.crawl", "args": {"url": "https://docs.site.com", "limit": 20}}',
    notes="⚠️ Resource intensive. Saves crawl JSON to SANDBOX/. Read the saved file for full content."
)
def crawl_website(url: str, limit: int = 50, max_depth: int = 3):
    """
    Crawl entire website recursively
    
    Args:
        url: Root URL to start crawling
        limit: Max pages to crawl
        max_depth: Max depth from root
    
    Returns:
        dict: Crawl results with page count and saved file path
    """
    try:
        client = get_firecrawl_client()
        
        print(f"[FIRECRAWL] Starting crawl of {url} (limit: {limit}, depth: {max_depth})...")
        
        # Anti-bot scrape options (snake_case for ScrapeOptions)
        scrape_options = {
            "formats": ["markdown"],
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            "skip_tls_verification": True,
            "timeout": 60000
        }

        response = client.crawl(
            url,
            limit=limit,
            scrape_options=scrape_options,
            poll_interval=10  # Check status every 10 seconds
        )
        
        # response can be a dict or a CrawlStatus object in v4
        pages = []
        if isinstance(response, dict):
            pages = response.get("data", [])
        elif hasattr(response, 'data'):
            pages = response.data
            
        # Save to SANDBOX folder
        output_dir = Path(__file__).parent.parent.parent / "SANDBOX"
        output_dir.mkdir(exist_ok=True)
        
        # Create filename from URL
        safe_url = url.replace("https://", "").replace("http://", "").replace("/", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"crawl_{safe_url}_{timestamp}.json"
        
        # Save full crawl data
        crawl_data = {
            "url": url,
            "crawled_at": datetime.now().isoformat(),
            "total_pages": len(pages),
            "pages": [
                {
                    "url": getattr(page, 'url', None) or (page.get('url') if isinstance(page, dict) else None),
                    "markdown": getattr(page, 'markdown', None) or (page.get('markdown') if isinstance(page, dict) else None),
                    "metadata": getattr(page, 'metadata', None) or (page.get('metadata') if isinstance(page, dict) else None)
                }
                for page in pages
            ]
        }
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(crawl_data, f, indent=2, default=str, ensure_ascii=False)
        
        print(f"[FIRECRAWL] Crawl complete! Saved to {output_file.name}")
        
        return {
            "success": True,
            "url": url,
            "pages_crawled": len(pages),
            "saved_to": str(output_file),
            "filename": output_file.name,
            "preview": [
                {"url": getattr(page, 'url', None) or (page.get('url') if isinstance(page, dict) else None)}
                for page in pages[:5]
            ]
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to crawl {url}: {str(e)}",
            "url": url
        }


@register_tool(
    "firecrawl.map",
    category="Web Scraping",
    description="Discover all URLs on a website (no content scraping)",
    example='{"tool": "firecrawl.map", "args": {"url": "https://example.com"}}',
    notes="⚠️ Result truncated to 200 chars in history. Saves full list to a .txt file automatically."
)
def map_website(url: str):
    """
    Map all URLs on a website
    
    Args:
        url: Website URL to map
    
    Returns:
        dict: List of discovered URLs
    """
    try:
        client = get_firecrawl_client()
        
        print(f"[FIRECRAWL] Mapping URLs on {url}...")
        
        result = client.map(url)
        # result is a MapData object with .links attribute
        links = result.links if hasattr(result, 'links') else []
        
        print(f"[FIRECRAWL] Found {len(links)} URLs")
        
        # Auto-save to file if there are many URLs (to avoid context overflow)
        saved_file = None
        if len(links) > 20:
            from pathlib import Path
            import json
            from datetime import datetime
            
            output_dir = Path(__file__).parent.parent.parent / "SANDBOX"
            output_dir.mkdir(exist_ok=True)
            
            safe_url = url.replace("https://", "").replace("http://", "").replace("/", "_")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = output_dir / f"map_{safe_url}_{timestamp}.txt"
            
            with open(output_file, 'w', encoding='utf-8') as f:
                for link in links:
                    f.write(f"{link}\n")
            
            saved_file = str(output_file)
            print(f"[FIRECRAWL] Saved {len(links)} URLs to {output_file.name}")
        
        return {
            "success": True,
            "url": url,
            "total_urls": len(links),
            "urls": links if len(links) <= 20 else links[:10],  # Only include sample if too many
            "saved_to": saved_file,
            "note": f"Full list of {len(links)} URLs saved to file (showing first 10)" if saved_file else None
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to map {url}: {str(e)}",
            "url": url
        }


@register_tool(
    "firecrawl.search",
    category="Web Scraping",
    description="Search web and automatically scrape results into markdown",
    example='{"tool": "firecrawl.search", "args": {"query": "deep research topic", "limit": 5}}',
    notes="Returns a list of results, each containing the full page content in markdown."
)
def search_web(query: str, limit: int = 5):
    """
    Search and scrape results
    
    Args:
        query: Search query
        limit: Number of results
    
    Returns:
        dict: Search results with scraped content
    """
    try:
        client = get_firecrawl_client()
        
        print(f"[FIRECRAWL] Searching for '{query}' (limit: {limit})...")
        
        result = client.search(query, limit=limit)
        # result is a SearchData object with .data attribute (list of Documents)
        results = result.data if hasattr(result, 'data') else []
        
        print(f"[FIRECRAWL] Found and scraped {len(results)} results")
        
        return {
            "success": True,
            "query": query,
            "total_results": len(results),
            "results": [
                {
                    "url": getattr(r, 'url', None) or getattr(r.metadata, 'sourceURL', None) if hasattr(r, 'metadata') else None,
                    "title": r.metadata.title if hasattr(r, 'metadata') and r.metadata else None,
                    "description": r.metadata.description if hasattr(r, 'metadata') and r.metadata else None,
                    "markdown": r.markdown[:500] + "..." if hasattr(r, 'markdown') and r.markdown and len(r.markdown) > 500 else getattr(r, 'markdown', "")
                }
                for r in results
            ]
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to search for '{query}': {str(e)}",
            "query": query
        }


@register_tool(
    "firecrawl.extract",
    category="Web Scraping",
    description="AI-powered structured data extraction using a JSON schema",
    example='{"tool": "firecrawl.extract", "args": {"url": "https://products.com", "schema": {"products": [{"name": "string", "price": "number"}]}}}',
    notes="⚠️ Requires OpenAI API key configured in Firecrawl backend."
)
def extract_structured(url: str, schema: dict):
    """
    AI-powered structured data extraction
    
    Args:
        url: URL to extract from
        schema: JSON schema for extraction
    
    Returns:
        dict: Extracted structured data
    """
    try:
        client = get_firecrawl_client()
        
        print(f"[FIRECRAWL] Extracting structured data from {url}...")
        
        # Note: This requires OpenAI API key in Firecrawl config
        result = client.scrape(
            url,
            formats=[{"type": "json", "schema": schema}]
        )
        
        extracted_data = result.json if hasattr(result, 'json') else {}
        
        print(f"[FIRECRAWL] Extraction complete")
        
        return {
            "success": True,
            "url": url,
            "extracted_data": extracted_data,
            "schema_used": schema
        }
    except Exception as e:
        error_msg = str(e)
        
        # Helpful error messages
        if "openai" in error_msg.lower() or "api key" in error_msg.lower():
            return {
                "success": False,
                "error": "AI extraction requires OpenAI API key. Add OPENAI_API_KEY to Firecrawl's .env file.",
                "url": url
            }
        
        return {
            "success": False,
            "error": f"Failed to extract from {url}: {error_msg}",
            "url": url
        }


print("[FIRECRAWL] Connector loaded - 6 tools registered")
print("[FIRECRAWL] Tools: scrape, crawl, map, search, extract, search_cache")
print("[FIRECRAWL] Ensure Firecrawl is running at http://localhost:3100")
