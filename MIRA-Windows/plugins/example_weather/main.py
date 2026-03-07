"""
Example Weather Plugin for MIRA
Demonstrates how to create a plugin that registers tools.
Uses wttr.in for weather data (no API key needed).
"""
import sys
from pathlib import Path

# Add parent paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from mcp_server.tools import register_tool


@register_tool(
    "weather.current",
    category="Weather (Plugin)",
    description="Get current weather for a city",
    example='{"tool": "weather.current", "args": {"city": "Delhi"}}'
)
def get_weather(city: str) -> dict:
    """
    Get current weather for a city using wttr.in.
    
    Args:
        city: City name (e.g., "Delhi", "New York", "London")
    
    Returns:
        Dict with weather information
    """
    try:
        import requests
        
        # wttr.in provides free weather data with no API key
        url = f"https://wttr.in/{city}?format=j1"
        response = requests.get(url, timeout=10, headers={"User-Agent": "MIRA-Agent"})
        response.raise_for_status()
        
        data = response.json()
        current = data.get("current_condition", [{}])[0]
        
        return {
            "city": city,
            "temperature_c": current.get("temp_C", "?"),
            "temperature_f": current.get("temp_F", "?"),
            "feels_like_c": current.get("FeelsLikeC", "?"),
            "humidity": current.get("humidity", "?") + "%",
            "description": current.get("weatherDesc", [{}])[0].get("value", "Unknown"),
            "wind_speed_kmh": current.get("windspeedKmph", "?"),
            "wind_direction": current.get("winddir16Point", "?"),
            "visibility_km": current.get("visibility", "?"),
            "uv_index": current.get("uvIndex", "?")
        }
        
    except ImportError:
        return {"status": "error", "error": "requests library not installed"}
    except Exception as e:
        return {"status": "error", "error": f"Weather lookup failed: {str(e)}"}


@register_tool(
    "weather.forecast",
    category="Weather (Plugin)",
    description="Get 3-day weather forecast for a city",
    example='{"tool": "weather.forecast", "args": {"city": "Mumbai"}}'
)
def get_forecast(city: str) -> dict:
    """
    Get 3-day weather forecast for a city.
    
    Args:
        city: City name
    
    Returns:
        Dict with forecast data
    """
    try:
        import requests
        
        url = f"https://wttr.in/{city}?format=j1"
        response = requests.get(url, timeout=10, headers={"User-Agent": "MIRA-Agent"})
        response.raise_for_status()
        
        data = response.json()
        weather = data.get("weather", [])
        
        forecast = []
        for day in weather[:3]:
            forecast.append({
                "date": day.get("date", "?"),
                "max_temp_c": day.get("maxtempC", "?"),
                "min_temp_c": day.get("mintempC", "?"),
                "description": day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "Unknown") if day.get("hourly") else "Unknown",
                "chance_of_rain": day.get("hourly", [{}])[4].get("chanceofrain", "?") + "%" if day.get("hourly") else "?"
            })
        
        return {
            "city": city,
            "forecast": forecast
        }
        
    except ImportError:
        return {"status": "error", "error": "requests library not installed"}
    except Exception as e:
        return {"status": "error", "error": f"Forecast lookup failed: {str(e)}"}
