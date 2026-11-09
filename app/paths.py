"""Locations of bundled resources and per-user data."""
import os
import sys
from pathlib import Path

APP_NAME = "ReviewKit"


def resource_dir() -> Path:
    """Folder holding skills/ and app/static (PyInstaller unpacks to _MEIPASS)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home())
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


SKILLS_DIR = resource_dir() / "skills"
STATIC_DIR = resource_dir() / "app" / "static"


def bundled_cli() -> Path | None:
    """claude.exe shipped inside claude-agent-sdk (also collected into the exe)."""
    candidates = [resource_dir() / "claude_agent_sdk" / "_bundled" / "claude.exe"]
    try:
        import claude_agent_sdk

        candidates.append(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude.exe")
    except Exception:
        pass
    for c in candidates:
        if c.exists():
            return c
    return None
