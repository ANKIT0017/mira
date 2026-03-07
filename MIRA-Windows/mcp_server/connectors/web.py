"""
Web scraper/reader connector for MIRA
Provides tools to fetch, read, and summarize web content
"""
import requests
from bs4 import BeautifulSoup
from readability import Document
import html2text
from ..tools import register_tool

# HTTP request configuration
DEFAULT_TIMEOUT = 15  # Increased for slower sites
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0'
}

@register_tool(
    "web.fetch",
    category="Web & Search",
    description="Fetch raw HTML and text from a URL",
    example='{"tool": "web.fetch", "args": {"url": "https://example.com"}}'
)
def fetch_url(url: str) -> dict:
    """
    Fetch raw HTML and text from a URL.
    
    Args:
        url: The URL to fetch
    
    Returns:
        Dict with html, text, title, and status code
    """
    try:
        print(f"[WEB] Fetching {url}...")
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Extract title
        title = soup.title.string if soup.title else "No title"
        
        # Get clean text
        for script in soup(["script", "style"]):
            script.decompose()
        text = soup.get_text(separator='\n', strip=True)
        
        return {
            "url": url,
            "title": title,
            "html": response.text[:5000],  # Limit to avoid overwhelming
            "text": text[:5000],
            "status_code": response.status_code
        }
        
    except requests.Timeout:
        return {"status": "error", "error": f"Request timed out after {DEFAULT_TIMEOUT}s"}
    except requests.RequestException as e:
        return {"status": "error", "error": f"Failed to fetch URL: {str(e)}"}
    except Exception as e:
        return {"status": "error", "error": f"Unexpected error: {str(e)}"}


@register_tool(
    "web.read",
    category="Web & Search",
    description="Fetch and extract clean article content from a URL (removes ads/clutter)",
    example='{"tool": "web.read", "args": {"url": "https://example.com/article"}}',
    notes="⚠️ For JavaScript-heavy sites, use firecrawl.scrape instead."
)
def read_article(url: str) -> dict:
    """
    Fetch and extract clean article content from a URL.
    Removes ads, navigation, and other clutter.
    
    Args:
        url: The URL of the article to read
    
    Returns:
        Dict with title, content (markdown), and metadata
    """
    try:
        print(f"[WEB] Reading article from {url}...")
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        
        # Explicitly set encoding to UTF-8 to avoid garbled text
        if response.encoding is None or response.encoding == 'ISO-8859-1':
            response.encoding = 'utf-8'
        
        # Get the decoded text, with error handling for bad encoding
        try:
            html_content = response.text
        except UnicodeDecodeError:
            # Fallback: decode with errors ignored
            html_content = response.content.decode('utf-8', errors='ignore')
        
        # Extract main content using readability
        doc = Document(html_content)
        
        # Convert HTML to markdown for cleaner display
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.ignore_emphasis = False
        h.body_width = 0  # Don't wrap lines
        
        content_html = doc.summary()
        content_markdown = h.handle(content_html)
        
        # Limit content length to avoid overwhelming context
        max_length = 3000
        if len(content_markdown) > max_length:
            content_markdown = content_markdown[:max_length] + "\n\n... (content truncated)"
        
        return {
            "url": url,
            "title": doc.title(),
            "content": content_markdown,
            "content_length": len(content_markdown)
        }
        
    except requests.Timeout:
        return {"status": "error", "error": f"Request timed out after {DEFAULT_TIMEOUT}s"}
    except requests.RequestException as e:
        return {"status": "error", "error": f"Failed to fetch article: {str(e)}"}
    except Exception as e:
        return {"status": "error", "error": f"Failed to extract article: {str(e)}"}


@register_tool(
    "web.summarize",
    category="Web & Search",
    description="Fetch and generate a concise AI summary of a URL",
    example='{"tool": "web.summarize", "args": {"url": "https://example.com/long-post", "max_length": 300}}',
    notes="⚠️ Automatically extracts content and uses an LLM to summarize."
)
def summarize_article(url: str, max_length: int = 200, model: str = "groq:llama-3.3-70b-versatile") -> dict:
    """
    Fetch, extract, and summarize an article from a URL.
    Uses LLM to generate a concise summary.
    
    Args:
        url: The URL of the article to summarize
        max_length: Maximum length of summary in words (default: 200)
        model: LLM model to use for summarization (default: "qwen2.5:3b" for speed)
    
    Returns:
        Dict with title, summary, and original URL
    """
    try:
        # First, read the article
        article = read_article(url)
        if not isinstance(article, dict):
            return {"status": "error", "error": "Failed to summarize article: invalid reader response"}
        if article.get("status") == "error":
            return article
        article_title = article.get("title", "").strip() or "Untitled"
        article_content = article.get("content", "")
        if not article_content:
            return {"status": "error", "error": "Failed to summarize article: empty extracted content"}
        
        # Import model_adapter using absolute import
        try:
            import sys
            import os
            # Add parent directories to path to ensure we can import agent module
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(current_dir, '..', '..'))
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            
            from agent.model_adapter import generate
        except ImportError as e:
            return {
                "status": "error", 
                "error": f"Could not import model_adapter: {str(e)}. Make sure agent/model_adapter.py exists."
            }
        
        # Create summarization prompt
        prompt = f"""Summarize the following article in {max_length} words or less. Be concise and capture the main points.

Article Title: {article_title}

Article Content:
{article_content[:5000]}

Summary:"""
        
        print(f"[WEB] Generating summary for {url} using model {model}...")
        summary = generate(prompt, model=model, max_tokens=max_length * 2)
        
        return {
            "url": url,
            "title": article_title,
            "summary": summary.strip(),
            "original_length": article.get('content_length', len(article_content)),
            "model_used": model
        }
        
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"[WEB] Summarize error: {error_detail}")
        return {"status": "error", "error": f"Failed to summarize article: {str(e)}", "detail": error_detail}
