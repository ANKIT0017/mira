"""
Plugin management tools for MIRA
Allows the agent to list, install, and remove plugins.
"""
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from ..tools import register_tool

# Plugin install/remove are gated behind an explicit opt-in env var.
# Plugin code runs with the same privileges as the MIRA process (arbitrary execution).
# Only enable this if you trust everything in your plugins/ directory.
# Set MIRA_ALLOW_PLUGIN_INSTALL=1 in .env to unlock these tools.
_PLUGIN_INSTALL_ALLOWED = os.environ.get("MIRA_ALLOW_PLUGIN_INSTALL", "0") == "1"


@register_tool(
    "plugin.list",
    category="Plugin Management",
    description="List all installed MIRA plugins and their status",
    example='{"tool": "plugin.list", "args": {}}'
)
def list_plugins() -> dict:
    """
    List all installed plugins.
    
    Returns:
        Dict with plugin list and counts
    """
    try:
        from mcp_server.plugin_manager import get_plugin_manager
        manager = get_plugin_manager()
        plugins = manager.list_plugins()
        
        return {
            "status": "ok",
            "plugins": plugins,
            "total": len(plugins),
            "loaded": sum(1 for p in plugins if p["status"] == "loaded"),
            "failed": sum(1 for p in plugins if p["status"] == "failed")
        }
    except Exception as e:
        return {"status": "error", "error": f"Failed to list plugins: {str(e)}"}


@register_tool(
    "plugin.install",
    category="Plugin Management",
    description="Install a plugin from a local directory path",
    example='{"tool": "plugin.install", "args": {"source": "SANDBOX/my_plugin"}}'
)
def install_plugin(source: str) -> dict:
    """
    Install a plugin from a local path.

    ⚠️  Requires MIRA_ALLOW_PLUGIN_INSTALL=1 in .env.
    Plugin code is executed with full MIRA process privileges.

    Args:
        source: Path to the plugin directory (must contain plugin.yaml)
    
    Returns:
        Dict with installation status
    """
    if not _PLUGIN_INSTALL_ALLOWED:
        return {
            "status": "error",
            "error": (
                "plugin.install is disabled by default because plugins run with full process "
                "privileges (arbitrary code execution). "
                "Set MIRA_ALLOW_PLUGIN_INSTALL=1 in .env to enable this tool."
            )
        }
    try:
        from mcp_server.plugin_manager import get_plugin_manager
        manager = get_plugin_manager()
        return manager.install_plugin(source)
    except Exception as e:
        return {"status": "error", "error": f"Plugin install failed: {str(e)}"}


@register_tool(
    "plugin.remove",
    category="Plugin Management",
    description="Remove an installed plugin by name",
    example='{"tool": "plugin.remove", "args": {"name": "example_weather"}}'
)
def remove_plugin(name: str) -> dict:
    """
    Remove a plugin by name.

    ⚠️  Requires MIRA_ALLOW_PLUGIN_INSTALL=1 in .env.

    Args:
        name: Name of the plugin to remove
    
    Returns:
        Dict with removal status
    """
    if not _PLUGIN_INSTALL_ALLOWED:
        return {
            "status": "error",
            "error": "plugin.remove is disabled. Set MIRA_ALLOW_PLUGIN_INSTALL=1 in .env to enable."
        }
    try:
        from mcp_server.plugin_manager import get_plugin_manager
        manager = get_plugin_manager()
        return manager.remove_plugin(name)
    except Exception as e:
        return {"status": "error", "error": f"Plugin removal failed: {str(e)}"}
