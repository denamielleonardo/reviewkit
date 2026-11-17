"""Small JSON settings file in %APPDATA%\\ReviewKit.

Only values that were actually set are written to the file, so changing a default in a new version
takes effect for everyone who never chose a value themselves.
"""
import json

from .paths import data_dir

_FILE = data_dir() / "settings.json"
DEFAULTS = {
    "repos": [], "last_repo": "", "api_key": "", "model": "", "theme": "system",
    "auto_update": True, "auto_archive": False, "archive_dir": "",
    "work_email": "",  # API-key users only: who they are in the activity log (Claude sign-in gives it otherwise)
    # Pull request + review request email, remembered for every repository once entered.
    "pr_to": "", "pr_greeting": "Sir", "pr_reviewers": "",
    "pr_people": [],  # people added with "Other…": [{"email", "name"}]
}


def _stored() -> dict:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # Earlier test builds wrote every default to the file, freezing the old greeting default.
    # A "Sir Den" saved before reviewers existed was never typed by the user, so drop it.
    if data.get("pr_greeting") == "Sir Den" and "pr_reviewers" not in data:
        data.pop("pr_greeting")
    return data


def load() -> dict:
    return {**DEFAULTS, **_stored()}


def save(values: dict) -> dict:
    cur = _stored()
    cur.update({k: v for k, v in values.items() if k in DEFAULTS})
    _FILE.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return {**DEFAULTS, **cur}


def remember_repo(path: str) -> None:
    s = load()
    repos = [path] + [r for r in s["repos"] if r.lower() != path.lower()]
    save({"repos": repos[:10], "last_repo": path})
