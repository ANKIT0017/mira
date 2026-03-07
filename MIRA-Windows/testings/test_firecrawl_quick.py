"""Quick test of Firecrawl scrape functionality"""
from firecrawl import Firecrawl

print("Testing Firecrawl connection...")
client = Firecrawl(api_url="http://localhost:3002")

print("\nTesting crawl on example.com (limit 1)...")
try:
    # Use limit=1 for speed
    result = client.crawl("https://example.com", limit=1)
    print(f"✅ Crawl success! Result type: {type(result)}")
    print(f"Crawl result: {result}")
except Exception as e:
    print(f"❌ Crawl Error: {type(e).__name__}: {e}")

print("\nTesting map on example.com...")
try:
    result = client.map("https://example.com")
    print(f"✅ Map success! Result type: {type(result)}")
except Exception as e:
    print(f"❌ Map Error: {type(e).__name__}: {e}")

print("\nTesting search for 'MIRA AI'...")
try:
    result = client.search("MIRA AI", limit=2)
    print(f"✅ Search success! Result type: {type(result)}")
    print(f"Search results count: {len(result.get('data', [])) if isinstance(result, dict) else 'Unknown'}")
except Exception as e:
    print(f"❌ Search Error: {type(e).__name__}: {e}")
