"""Saved reviews: %APPDATA%\\ReviewKit\\history\\<id>.md plus index.json.

Reviews are kept for RETENTION_DAYS. Older ones are deleted, or moved to the developer's
archive folder when auto-archive is on.
"""
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from .paths import data_dir

RETENTION_DAYS = 7

_DIR = data_dir() / "history"
_DIR.mkdir(exist_ok=True)
_INDEX = _DIR / "index.json"
_LOCK = threading.RLock()
last_purge: dict = {"archived": 0, "deleted": 0, "error": ""}


def _read_index() -> list[dict]:
    try:
        return json.loads(_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_index(items: list[dict]) -> None:
    _INDEX.write_text(json.dumps(items, indent=2), encoding="utf-8")


def add(meta: dict, markdown: str) -> dict:
    entry = {"id": uuid.uuid4().hex[:12], "created": time.strftime("%Y-%m-%d %H:%M"), **meta}
    with _LOCK:
        (_DIR / f"{entry['id']}.md").write_text(markdown, encoding="utf-8")
        _write_index(([entry] + _read_index())[:500])
    return entry


def update(entry_id: str, fields: dict, markdown: str | None = None) -> dict | None:
    with _LOCK:
        items = _read_index()
        for e in items:
            if e["id"] == entry_id:
                e.update(fields)
                _write_index(items)
                if markdown is not None:
                    (_DIR / f"{entry_id}.md").write_text(markdown, encoding="utf-8")
                return e
    return None


def file_path(entry_id: str, suffix: str):
    """Path for a file stored with a history entry, e.g. suffix 'source.docx'."""
    return _DIR / f"{entry_id}.{suffix}"


def list_all() -> list[dict]:
    return _read_index()


def get(entry_id: str) -> dict | None:
    for e in _read_index():
        if e["id"] == entry_id:
            p = _DIR / f"{entry_id}.md"
            return {**e, "markdown": p.read_text(encoding="utf-8") if p.exists() else ""}
    return None


def _remove_files(entry_id: str) -> None:
    for f in _DIR.glob(f"{entry_id}.*"):
        f.unlink(missing_ok=True)


def delete(entry_id: str) -> None:
    with _LOCK:
        _write_index([e for e in _read_index() if e["id"] != entry_id])
        _remove_files(entry_id)


# ---------- retention / archive ----------

def _created(e: dict) -> float:
    try:
        return time.mktime(time.strptime(e.get("created", ""), "%Y-%m-%d %H:%M"))
    except ValueError:
        return time.time()  # unknown date: keep it


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ .()" else "_" for c in name).strip()[:80] or "review"


def check_archive_dir(path: str) -> Path:
    """Create the archive folder if needed and make sure we can write to it."""
    folder = Path(path.strip().strip('"')).expanduser()
    if not folder.is_absolute():
        raise ValueError("Enter a full folder path, e.g. D:\\ReviewKit archive.")
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / f".reviewkit-{uuid.uuid4().hex[:6]}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return folder


def _archive(e: dict, root: Path) -> None:
    folder = root / (e.get("created", "")[:7] or "undated")  # one folder per month: 2026-11
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{e.get('created', '')[:10]} {_safe(e.get('title', 'review'))} [{e['id']}]"
    md = _DIR / f"{e['id']}.md"
    body = md.read_text(encoding="utf-8") if md.exists() else ""
    header = (f"<!-- ReviewKit archive · {e.get('label', '')} · {e.get('created', '')} · "
              f"{e.get('title', '')} -->\n\n")
    (folder / f"{base}.md").write_text(header + body, encoding="utf-8")
    for f in _DIR.glob(f"{e['id']}.*"):  # attachments, e.g. <id>.source.docx
        if f.suffix != ".md":
            shutil.copy2(f, folder / f"{base}{f.name[len(e['id']):]}")


def purge(auto_archive: bool, archive_dir: str) -> dict:
    """Delete (or archive) reviews older than RETENTION_DAYS. Entries that fail to archive are kept."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    archived = deleted = 0
    error = ""
    root = None
    with _LOCK:
        items = _read_index()
        old = [e for e in items if _created(e) < cutoff]
        if not old:
            last_purge.update(archived=0, deleted=0, error="")
            return dict(last_purge)
        if auto_archive:
            try:
                root = check_archive_dir(archive_dir) if archive_dir.strip() else None
                if root is None:
                    error = "Auto-archive is on but no archive folder is set."
            except (OSError, ValueError) as ex:
                error = f"Archive folder unavailable: {ex}"
        gone: set[str] = set()
        for e in old:
            if auto_archive:
                if root is None:
                    continue  # never delete what we were asked to archive
                try:
                    _archive(e, root)
                    archived += 1
                except OSError as ex:
                    error = f"Could not archive “{e.get('title', '')}”: {ex}"
                    continue
            else:
                deleted += 1
            gone.add(e["id"])
        if gone:
            _write_index([e for e in items if e["id"] not in gone])
            for i in gone:
                _remove_files(i)
    last_purge.update(archived=archived, deleted=deleted, error=error)
    return dict(last_purge)
