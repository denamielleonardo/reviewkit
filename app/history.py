"""Saved reviews: %APPDATA%\\ReviewKit\\history\\<id>.md plus index.json."""
import json
import time
import uuid

from .paths import data_dir

_DIR = data_dir() / "history"
_DIR.mkdir(exist_ok=True)
_INDEX = _DIR / "index.json"


def _read_index() -> list[dict]:
    try:
        return json.loads(_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_index(items: list[dict]) -> None:
    _INDEX.write_text(json.dumps(items, indent=2), encoding="utf-8")


def add(meta: dict, markdown: str) -> dict:
    entry = {"id": uuid.uuid4().hex[:12], "created": time.strftime("%Y-%m-%d %H:%M"), **meta}
    (_DIR / f"{entry['id']}.md").write_text(markdown, encoding="utf-8")
    _write_index(([entry] + _read_index())[:500])
    return entry


def list_all() -> list[dict]:
    return _read_index()


def get(entry_id: str) -> dict | None:
    for e in _read_index():
        if e["id"] == entry_id:
            p = _DIR / f"{entry_id}.md"
            return {**e, "markdown": p.read_text(encoding="utf-8") if p.exists() else ""}
    return None


def delete(entry_id: str) -> None:
    _write_index([e for e in _read_index() if e["id"] != entry_id])
    (_DIR / f"{entry_id}.md").unlink(missing_ok=True)
