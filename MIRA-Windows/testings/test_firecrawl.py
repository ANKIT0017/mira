"""
Test script for Firecrawl connector
Run after installing firecrawl-py and starting Firecrawl Docker containers
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.append(str(Path(__file__).parent))

# Import tool execution system
from mcp_server.tools import execute_tool, list_tools

def test_firecrawl_tools():
    """Test all Firecrawl tools"""
    
    print("=" * 70)
    print("FIRECRAWL CONNECTOR TEST SUITE")
    print("=" * 70)
    
    # Check if Firecrawl tools are registered
    print("\n[Step 1] Checking registered tools...")
    all_tools = list_tools()
    firecrawl_tools = [t for t in all_tools if isinstance(t, dict) and t.get("name", "").startswith("firecrawl.")]
    
    if len(firecrawl_tools) == 0:
        print("❌ ERROR: No Firecrawl tools found!")
        print("   Make sure firecrawl.py connector is loaded by MCP server")
        return False
    
    print(f"✓ Found {len(firecrawl_tools)} Firecrawl tools:")
    for tool in firecrawl_tools:
        print(f"   - {tool['name']}")
    
    # Test 1: Scrape single page
    print("\n" + "=" * 70)
    print("[Test 1] Scraping single URL...")
    print("=" * 70)
    
    try:
        result = execute_tool("firecrawl.scrape", {
            "url": "https://firecrawl.dev",
            "formats": ["markdown"]
        })
        
        if result.get("success"):
            print("✓ Test 1 PASSED")
            print(f"   URL: {result.get('url')}")
            print(f"   Markdown length: {len(result.get('markdown', ''))} characters")
            print(f"   Title: {result.get('metadata', {}).get('title')}")
        else:
            print(f"❌ Test 1 FAILED: {result.get('error')}")
            return False
    except Exception as e:
        print(f"❌ Test 1 FAILED with exception: {e}")
        return False
    
    # Test 2: Map website URLs
    print("\n" + "=" * 70)
    print("[Test 2] Mapping website URLs...")
    print("=" * 70)
    
    try:
        result = execute_tool("firecrawl.map", {
            "url": "https://firecrawl.dev"
        })
        
        if result.get("success"):
            print("✓ Test 2 PASSED")
            print(f"   Total URLs found: {result.get('total_urls')}")
            print(f"   Sample URLs: {result.get('urls', [])[:3]}")
        else:
            print(f"❌ Test 2 FAILED: {result.get('error')}")
            return False
    except Exception as e:
        print(f"❌ Test 2 FAILED with exception: {e}")
        return False
    
    # Test 3: Crawl small website
    print("\n" + "=" * 70)
    print("[Test 3] Crawling website (limit 3)...")
    print("=" * 70)
    
    try:
        result = execute_tool("firecrawl.crawl", {
            "url": "https://firecrawl.dev",
            "limit": 3,
            "max_depth": 1
        })
        
        if result.get("success"):
            print("✓ Test 3 PASSED")
            print(f"   Pages crawled: {result.get('pages_crawled')}")
            print(f"   Saved to: {result.get('filename')}")
            print(f"   Preview: {result.get('preview')}")
        else:
            print(f"❌ Test 3 FAILED: {result.get('error')}")
            return False
    except Exception as e:
        print(f"❌ Test 3 FAILED with exception: {e}")
        return False
    
    # Test 4: Search (optional - may require API key)
    print("\n" + "=" * 70)
    print("[Test 4] Web search (optional)...")
    print("=" * 70)
    
    try:
        result = execute_tool("firecrawl.search", {
            "query": "firecrawl documentation",
            "limit": 2
        })
        
        if result.get("success"):
            print("✓ Test 4 PASSED")
            print(f"   Results found: {result.get('total_results')}")
        else:
            print(f"⚠ Test 4 SKIPPED or FAILED: {result.get('error')}")
            print("   (This is optional - may require additional configuration)")
    except Exception as e:
        print(f"⚠ Test 4 SKIPPED: {e}")
        print("   (This is optional)")
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print("✓ All critical tests passed!")
    print("\nFirecrawl integration is working correctly.")
    print("You can now use Firecrawl tools via MIRA's web UI or voice interface.")
    print("=" * 70)
    
    return True


if __name__ == "__main__":
    import sys
    
    print("\nFirecrawl Test Script")
    print("Ensure the following before running:")
    print("1. Firecrawl Docker containers are running (docker ps)")
    print("2. firecrawl-py is installed (pip install firecrawl-py)")
    print("3. MCP server has loaded the connector\n")
    
    input("Press Enter to start tests...")
    
    try:
        success = test_firecrawl_tools()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
