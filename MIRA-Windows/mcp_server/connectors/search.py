"""
DuckDuckGo search connector for MIRA
Provides web search and news search without API keys
"""
from ..tools import register_tool

@register_tool(
    "search.duckduckgo",
    category="Web & Search",
    description="Search the web using DuckDuckGo",
    example='{"tool": "search.duckduckgo", "args": {"query": "delhi software jobs 2026", "max_results": 10}}'
)
def search_duckduckgo(query: str, max_results: int = 5) -> dict:
    """
    Search the web using DuckDuckGo.
    
    Args:
        query: Search query
        max_results: Maximum number of results to return (default: 5)
    
    Returns:
        Dict with search results
    """
    try:
        print(f"[SEARCH] DuckDuckGo search: '{query}'")
        
        # Use the new ddgs library (duckduckgo_search was renamed)
        from ddgs import DDGS
        
        results = []
        with DDGS() as ddgs:
            # Use text() method for regular search
            search_results = ddgs.text(query, max_results=max_results)
            
            for r in search_results:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "")
                })
        
        print(f"[SEARCH] Found {len(results)} results")
        return {
            "query": query,
            "results": results,
            "count": len(results)
        }
        
    except ImportError:
        return {
            "status": "error",
            "error": "ddgs library not installed. Run: pip install ddgs"
        }
    except Exception as e:
        print(f"[SEARCH] Error: {str(e)}")
        return {
            "status": "error",
            "error": f"Search failed: {str(e)}"
        }


@register_tool(
    "search.news",
    category="Web & Search",
    description="Search for the latest news stories",
    example='{"tool": "search.news", "args": {"query": "latest AI breakthroughs", "max_results": 5}}'
)
def search_news(query: str, max_results: int = 5) -> dict:
    """
    Search for news articles using DuckDuckGo News.
    
    Args:
        query: Search query
        max_results: Maximum number of results to return (default: 5)
    
    Returns:
        Dict with news results
    """
    try:
        print(f"[SEARCH] DuckDuckGo news search: '{query}')")
        
        from ddgs import DDGS
        
        results = []
        with DDGS() as ddgs:
            # Use news() method for news search
            news_results = ddgs.news(query, max_results=max_results)
            
            for r in news_results:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "source": r.get("source", ""),
                    "date": r.get("date", ""),
                    "snippet": r.get("body", "")
                })
        
        print(f"[SEARCH] Found {len(results)} news results")
        return {
            "query": query,
            "results": results,
            "count": len(results)
        }
        
    except ImportError:
        return {
            "status": "error",
            "error": "ddgs library not installed. Run: pip install ddgs"
        }
    except Exception as e:
        print(f"[SEARCH] Error: {str(e)}")
        return {
            "status": "error",
            "error": f"News search failed: {str(e)}"
        }
