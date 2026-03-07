# MIRA Plugins

Drop plugin folders here. Each plugin needs:

1. A `plugin.yaml` manifest
2. A Python entry point (default: `main.py`)

## Plugin Structure

```
plugins/
├── my_plugin/
│   ├── plugin.yaml    # Required manifest
│   └── main.py        # Entry point (registers tools)
```

## plugin.yaml Format

```yaml
name: my_plugin
version: 1.0.0
description: What this plugin does
author: Your Name
entry_point: main.py
dependencies:
  - requests>=2.28.0
```

## Creating a Plugin

Your entry point file just needs to import `register_tool` and define functions:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_server"))
from mcp_server.tools import register_tool

@register_tool(
    "myplugin.action",
    category="My Plugin",
    description="Does something useful"
)
def my_action(param: str) -> dict:
    return {"result": f"Did something with {param}"}
```

Plugins are auto-loaded when MIRA starts. Dependencies listed in `plugin.yaml` are auto-installed.
