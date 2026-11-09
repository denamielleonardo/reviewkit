"""Small JSON settings file in %APPDATA%\\ReviewKit."""
import json

from .paths import data_dir

_FILE = data_dir() / "settings.json"
DEFAULTS = {
    "repos": [], "last_repo": "", "api_key": "", "model": "", "theme": "system",
    "auto_update": True, "auto_archive": False, "archive_dir": "",
}


def load() -> dict:
    try:
        return {**DEFAULTS, **json.loads(_FILE.read_text(encoding="utf-8"))}
    except Exception:
        return dict(DEFAULTS)


def save(values: dict) -> dict:
    cur = load()
    cur.update({k: v for k, v in values.items() if k in DEFAULTS})
    _FILE.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def remember_repo(path: str) -> None:
    s = load()
    repos = [path] + [r for r in s["repos"] if r.lower() != path.lower()]
    save({"repos": repos[:10], "last_repo": path})
