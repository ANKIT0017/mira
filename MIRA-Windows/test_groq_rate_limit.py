import os
import requests
from dotenv import load_dotenv

load_dotenv()

def test_groq_api():
    """Test if Groq API is actually rate limited."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("❌ GROQ_API_KEY not found in .env")
        return False
    
    print(f"🔑 Using API key: {api_key[:20]}...{api_key[-4:]}")
    
    base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "qwen/qwen3-32b",
        "messages": [{"role": "user", "content": "Say 'API working' in 2 words"}],
        "max_tokens": 10,
        "temperature": 0.7
    }
    
    print(f"\n📡 Testing Groq API at {base_url}/chat/completions")
    print(f"📦 Model: qwen/qwen3-32b")
    print(f"🔄 Sending test request...\n")
    
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        
        print(f"📊 Response Status: {response.status_code}")
        
        if response.status_code == 429:
            print("❌ CONFIRMED: API is rate limited (HTTP 429)")
            print(f"Response: {response.text}")
            return False
        elif response.status_code == 200:
            data = response.json()
            result = data["choices"][0]["message"]["content"]
            print(f"✅ SUCCESS: API is working!")
            print(f"Response: {result}")
            return True
        else:
            print(f"⚠️ Unexpected status code: {response.status_code}")
            print(f"Response: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

if __name__ == "__main__":
    print("="*60)
    print("  GROQ API RATE LIMIT TEST")
    print("="*60)
    
    working = test_groq_api()
    
    print("\n" + "="*60)
    if working:
        print("✅ RESULT: Groq API is NOT rate limited")
        print("\n💡 The issue may be in the model_adapter.py logic")
        print("   or there might be request header/payload issues.")
    else:
        print("❌ RESULT: Groq API is genuinely rate limited")
        print("\n💡 Wait 60 seconds or use Gemini/Local models")
    print("="*60)
