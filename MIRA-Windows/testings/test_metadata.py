"""Test the enhanced metadata-driven tool documentation"""
import sys
sys.path.append('.')

# Import all connectors to register tools
from mcp_server.connectors import *
from mcp_server.tools import generate_tool_documentation, get_tools_by_category

print("=" * 80)
print("TOOLS BY CATEGORY:")
print("=" * 80)

categorized = get_tools_by_category()
for category, tools in categorized.items():
    print(f"\n{category}: {len(tools)} tools")
    for tool in tools:
        print(f"  - {tool['name']}")

print("\n" + "=" * 80)
print("AUTO-GENERATED DOCUMENTATION:")
print("=" * 80)
print(generate_tool_documentation())
