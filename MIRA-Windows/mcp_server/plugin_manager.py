"""
Plugin Manager for MIRA
Loads, validates, and manages plugins from the plugins/ directory.
"""
import os
import sys
import importlib
import subprocess
from pathlib import Path

# Base plugins directory
PLUGINS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins")

class PluginManager:
    """Manages plugin discovery, loading, and lifecycle."""
    
    def __init__(self):
        self.plugins = {}  # name -> plugin info dict
        self.failed = {}   # name -> error message
    
    def load_all(self):
        """Discover and load all plugins from the plugins/ directory."""
        if not os.path.isdir(PLUGINS_DIR):
            os.makedirs(PLUGINS_DIR, exist_ok=True)
            print(f"[PLUGINS] Created plugins directory: {PLUGINS_DIR}")
            return
        
        for entry in os.scandir(PLUGINS_DIR):
            if entry.is_dir() and not entry.name.startswith((".", "_")):
                self._load_plugin(entry.path)
        
        loaded = len(self.plugins)
        failed = len(self.failed)
        print(f"[PLUGINS] Loaded {loaded} plugin(s), {failed} failed")
    
    def _load_plugin(self, plugin_dir: str):
        """Load a single plugin from its directory."""
        manifest_path = os.path.join(plugin_dir, "plugin.yaml")
        
        if not os.path.exists(manifest_path):
            self.failed[os.path.basename(plugin_dir)] = "No plugin.yaml found"
            return
        
        try:
            # Parse YAML manifest (use simple parser to avoid PyYAML dependency)
            manifest = self._parse_yaml(manifest_path)
            
            name = manifest.get("name", os.path.basename(plugin_dir))
            version = manifest.get("version", "0.0.0")
            description = manifest.get("description", "No description")
            entry_point = manifest.get("entry_point", "main.py")
            dependencies = manifest.get("dependencies", [])
            author = manifest.get("author", "Unknown")
            
            print(f"[PLUGINS] Loading plugin: {name} v{version}")
            
            # Install missing dependencies
            if dependencies:
                self._install_dependencies(name, dependencies)
            
            # Import the entry point module
            entry_path = os.path.join(plugin_dir, entry_point)
            if not os.path.exists(entry_path):
                self.failed[name] = f"Entry point not found: {entry_point}"
                return
            
            # Add plugin dir to path temporarily
            if plugin_dir not in sys.path:
                sys.path.insert(0, plugin_dir)
            
            module_name = f"plugin_{name}"
            spec = importlib.util.spec_from_file_location(module_name, entry_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            self.plugins[name] = {
                "name": name,
                "version": version,
                "description": description,
                "author": author,
                "path": plugin_dir,
                "entry_point": entry_point,
                "module": module,
                "status": "loaded"
            }
            
            print(f"[PLUGINS] ✅ Plugin loaded: {name} v{version}")
            
        except Exception as e:
            plugin_name = os.path.basename(plugin_dir)
            self.failed[plugin_name] = str(e)
            print(f"[PLUGINS] ❌ Failed to load plugin '{plugin_name}': {e}")
    
    def _install_dependencies(self, plugin_name: str, dependencies: list):
        """Install missing pip dependencies for a plugin."""
        for dep in dependencies:
            pkg_name = dep.split(">=")[0].split("==")[0].split("<=")[0].strip()
            try:
                importlib.import_module(pkg_name.replace("-", "_"))
            except ImportError:
                print(f"[PLUGINS] Installing dependency for {plugin_name}: {dep}")
                try:
                    subprocess.check_call(
                        [sys.executable, "-m", "pip", "install", dep, "--quiet"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                except subprocess.CalledProcessError as e:
                    print(f"[PLUGINS] Warning: Failed to install {dep}: {e}")
    
    def _parse_yaml(self, path: str) -> dict:
        """Simple YAML parser for plugin manifests (avoids PyYAML dependency)."""
        result = {}
        current_list_key = None
        
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                
                # Skip empty lines and comments
                if not line.strip() or line.strip().startswith("#"):
                    current_list_key = None
                    continue
                
                # List item
                if line.strip().startswith("- ") and current_list_key:
                    value = line.strip()[2:].strip().strip('"').strip("'")
                    if current_list_key not in result:
                        result[current_list_key] = []
                    result[current_list_key].append(value)
                    continue
                
                # Key-value pair
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    
                    if value:
                        result[key] = value
                    else:
                        # This might be a list key
                        current_list_key = key
                        result[key] = []
                    continue
        
        return result
    
    def get_plugin(self, name: str) -> dict:
        """Get info about a specific plugin."""
        return self.plugins.get(name)
    
    def list_plugins(self) -> list:
        """List all loaded plugins."""
        plugins = []
        for name, info in self.plugins.items():
            plugins.append({
                "name": info["name"],
                "version": info["version"],
                "description": info["description"],
                "author": info["author"],
                "status": info["status"]
            })
        for name, error in self.failed.items():
            plugins.append({
                "name": name,
                "version": "?",
                "description": f"Failed: {error}",
                "author": "?",
                "status": "failed"
            })
        return plugins
    
    def remove_plugin(self, name: str) -> dict:
        """Remove a plugin by deleting its directory."""
        import shutil
        
        if name in self.plugins:
            plugin_path = self.plugins[name]["path"]
            del self.plugins[name]
        elif name in self.failed:
            plugin_path = os.path.join(PLUGINS_DIR, name)
            del self.failed[name]
        else:
            return {"status": "error", "error": f"Plugin '{name}' not found"}
        
        if os.path.isdir(plugin_path):
            shutil.rmtree(plugin_path)
        
        return {"status": "success", "message": f"Plugin '{name}' removed"}
    
    def install_plugin(self, source: str) -> dict:
        """
        Install a plugin from a local path.
        Copies the plugin directory into plugins/ and loads it.
        """
        import shutil
        
        source_path = Path(source)
        if not source_path.is_dir():
            return {"status": "error", "error": f"Source directory not found: {source}"}
        
        manifest = os.path.join(source, "plugin.yaml")
        if not os.path.exists(manifest):
            return {"status": "error", "error": "No plugin.yaml found in source directory"}
        
        dest_name = source_path.name
        dest_path = os.path.join(PLUGINS_DIR, dest_name)
        
        if os.path.exists(dest_path):
            shutil.rmtree(dest_path)
        
        shutil.copytree(source, dest_path)
        self._load_plugin(dest_path)
        
        if dest_name in self.failed:
            return {"status": "error", "error": self.failed[dest_name]}
        
        return {"status": "success", "message": f"Plugin '{dest_name}' installed and loaded"}


# Global singleton
_manager = None

def get_plugin_manager() -> PluginManager:
    """Get or create the global plugin manager."""
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager
