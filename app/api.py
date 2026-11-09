"""HTTP API used by the ReviewKit window (served on 127.0.0.1 only)."""
import asyncio
import json
import os
import re
import shutil
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import claude_runner, detect, docx_editor, git_ops, history, settings, updater
from .docx_reader import docx_to_text
from .paths import STATIC_DIR
from .skills import REVIEWS

try:  # written by CI at build time
    from ._build import COMMIT, VERSION
except ImportError:
    VERSION, COMMIT = "dev", ""
app = FastAPI(title="ReviewKit", docs_url=None, redoc_url=None)

# Set by main.py when running inside a pywebview window (for native dialogs).
WINDOW = {"ref": None}


class RepoIn(BaseModel):
    path: str


class CodeReviewIn(BaseModel):
    repo: str
    source: str
    target: str
    review: str = ""  # ignored: the review type is detected from the repository and the change
    notes: str = ""


class SettingsIn(BaseModel):
    api_key: str | None = None
    model: str | None = None
    theme: str | None = None
    auto_update: bool | None = None
    auto_archive: bool | None = None
    archive_dir: str | None = None


def _err(e: Exception, code: int = 400):
    raise HTTPException(status_code=code, detail=str(e))


# ---------- connection / settings ----------

def _purge_history() -> dict:
    s = settings.load()
    return history.purge(bool(s.get("auto_archive")), s.get("archive_dir", ""))


@app.get("/api/status")
async def status():
    auth = await asyncio.to_thread(claude_runner.auth_status)
    await asyncio.to_thread(_purge_history)
    return {
        "version": VERSION,
        "git": shutil.which("git") is not None,
        "auth": auth,
        "reviews": [{"id": k, **v} for k, v in REVIEWS.items()],
    }


@app.post("/api/login")
async def login():
    try:
        claude_runner.start_login()
    except Exception as e:  # noqa: BLE001
        _err(e, 500)
    return {"ok": True}


@app.get("/api/login/wait")
async def login_wait():
    """Long-poll while the sign-in console is open; returns as soon as sign-in completes.

    The window's timers are throttled while the user is in the browser signing in, but a
    pending request still resolves, so the UI updates without a manual refresh.
    """
    auth: dict = {}
    for i in range(50):  # about 25 seconds
        if i % 8 == 0 or not claude_runner.login_running():
            auth = await asyncio.to_thread(claude_runner.auth_status)
            if auth.get("loggedIn") or not claude_runner.login_running():
                break
        await asyncio.sleep(0.5)
    return {"running": claude_runner.login_running(), "auth": auth}


@app.post("/api/logout")
async def logout():
    await asyncio.to_thread(claude_runner.logout)
    return {"ok": True}


@app.post("/api/test")
async def test():
    return await claude_runner.test_connection()


def _public_settings(s: dict) -> dict:
    key = s.get("api_key", "")
    return {**s, "api_key": "", "api_key_set": bool(key), "api_key_hint": f"…{key[-4:]}" if key else "",
            "retention_days": history.RETENTION_DAYS, "purge": dict(history.last_purge)}


@app.get("/api/settings")
async def get_settings():
    return _public_settings(settings.load())


@app.post("/api/settings")
async def set_settings(body: SettingsIn):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    if "archive_dir" in values:
        values["archive_dir"] = values["archive_dir"].strip().strip('"')
    archive_on = values.get("auto_archive", settings.load().get("auto_archive"))
    archive_dir = values.get("archive_dir", settings.load().get("archive_dir", ""))
    if ("auto_archive" in values or "archive_dir" in values) and archive_on:
        if not archive_dir:
            _err(ValueError("Choose a folder to archive old reviews into."))
        try:
            values["archive_dir"] = str(await asyncio.to_thread(history.check_archive_dir, archive_dir))
        except (OSError, ValueError) as e:
            _err(ValueError(f"Can't use that archive folder: {e}"))
    saved = settings.save(values)
    if "auto_archive" in values or "archive_dir" in values:
        await asyncio.to_thread(_purge_history)
    return _public_settings(saved)


# ---------- updates ----------

@app.get("/api/update")
async def check_update():
    try:
        return await asyncio.to_thread(updater.check, VERSION)
    except Exception as e:  # noqa: BLE001  offline, proxy, etc.
        return {"current": VERSION, "available": False, "error": f"Couldn't check for updates: {e}"}


def _quit_soon() -> None:
    """Close the app so the update script can replace its files."""
    def go():
        win = WINDOW["ref"]
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
        threading.Timer(3, lambda: os._exit(0)).start()
    threading.Timer(1.5, go).start()


@app.post("/api/update/install")
def install_update():
    def stream():
        try:
            for ev in updater.install():
                yield json.dumps(ev) + "\n"
                if ev["t"] == "done":
                    _quit_soon()
        except Exception as e:  # noqa: BLE001
            yield json.dumps({"t": "error", "v": str(e)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------- repos ----------

@app.post("/api/repo")
async def open_repo(body: RepoIn):
    try:
        root = await asyncio.to_thread(git_ops.repo_root, body.path.strip().strip('"'))
        info = await asyncio.to_thread(git_ops.repo_info, root)
        info["branches"] = await asyncio.to_thread(git_ops.branches, root)
        info["stack"] = detect.describe(await asyncio.to_thread(detect.scan, root))
    except git_ops.GitError as e:
        _err(e)
    settings.remember_repo(root)
    return info


@app.post("/api/repo/fetch")
async def fetch_repo(body: RepoIn):
    try:
        await asyncio.to_thread(git_ops.fetch, body.path)
        return {"branches": await asyncio.to_thread(git_ops.branches, body.path)}
    except git_ops.GitError as e:
        _err(e)


@app.post("/api/pick-folder")
async def pick_folder():
    win = WINDOW["ref"]
    if win is None:
        raise HTTPException(501, "Folder picker is only available in the desktop window; paste the path instead.")
    import webview

    result = await asyncio.to_thread(win.create_file_dialog, webview.FileDialog.FOLDER)
    return {"path": result[0] if result else ""}


# ---------- reviews ----------

def _ndjson(gen, meta: dict, on_done=None):
    """Stream events as NDJSON; on completion save to history (on_done may adjust first)."""
    async def stream():
        async for ev in gen:
            if ev["t"] == "done" and ev.get("markdown"):
                extra = on_done(ev) if on_done else {}
                entry = history.add({**meta, **extra.get("meta", {}), "seconds": ev.get("seconds")}, ev["markdown"])
                for suffix, data in extra.get("files", {}).items():
                    history.file_path(entry["id"], suffix).write_bytes(data)
                ev["entry"] = entry
            yield json.dumps(ev) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _stream_events(events):
    async def stream():
        async for ev in events:
            yield json.dumps(ev) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


POINTS_INSTRUCTION = """

After the review, append exactly one block in this format. It is hidden from the reader and is used to let the author apply accepted points to the document:
<!-- REVIEW_POINTS
[{"id": "RP1", "severity": "Critical|High|Medium|Low", "section": "<document section it concerns>", "title": "<short title>", "change": "<the concrete change to make in the document to resolve it>"}]
-->
Include every finding from your review, in the same order and with the same severity. Do not mention this block in the review text."""

_POINTS_RE = re.compile(r"<!--\s*REVIEW_POINTS\s*(.*?)-->", re.S)


def extract_points(markdown: str) -> tuple[str, list[dict]]:
    """Split the hidden REVIEW_POINTS block off the review; returns (clean markdown, points)."""
    m = _POINTS_RE.search(markdown)
    if not m:
        return markdown.strip(), []
    clean = (markdown[: m.start()] + markdown[m.end():]).strip()
    try:
        raw = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return clean, []
    points = []
    for i, p in enumerate(raw if isinstance(raw, list) else [], 1):
        if not isinstance(p, dict):
            continue
        points.append({
            "id": str(p.get("id") or f"RP{i}"),
            "severity": str(p.get("severity", "")),
            "section": str(p.get("section", "")),
            "title": str(p.get("title", "")),
            "change": str(p.get("change", "")),
            "status": "open",
        })
    return clean, points


def _code_prompt(info: dict, d: dict, body: CodeReviewIn, types: list[str], groups: dict) -> str:
    parts = [
        f"Review the pull request that merges **{body.source}** into **{body.target}** "
        f"in repository `{info['name']}`" + (f" ({info['origin']})" if info["origin"] else "") + ".",
        f"Use `{body.source}` as the branch name in the review header.",
        "Only review the changes in this diff. You may read surrounding code for context. "
        "The working tree may be checked out on another branch, so to see a file as it is on the "
        f"source branch use `git show {d['source_ref']}:<path>`.",
        f"## Commits\n```\n{d['commits'].strip() or '(none)'}\n```",
        f"## Changed files\n```\n{d['files'].strip()}\n```",
        f"## Diff stat\n```\n{d['stat'].strip()}\n```",
    ]
    if d["truncated"]:
        parts.append(
            "The full diff is too large to include. Inspect it yourself with "
            f"`git diff {d['range']} -- <path>`, file by file, prioritising source files over generated ones."
        )
    else:
        parts.append(f"## Full diff (`git diff {d['range']}`)\n```diff\n{d['diff']}\n```")
    if len(types) > 1:
        lines = ["## Files per review", "Review each group with its own rules:"]
        for t in types:
            files = groups.get(t, [])
            lines.append(f"\n### {REVIEWS[t]['label']} ({len(files)} files)\n" + "\n".join(f"- `{f}`" for f in files))
        parts.append("\n".join(lines))
    if body.notes.strip():
        parts.append(f"## Notes from the developer\n{body.notes.strip()}")
    return "\n\n".join(parts)


@app.post("/api/review/code/rules")
async def code_rules(body: CodeReviewIn):
    """Which review rules a change would get (shown in the confirmation before running)."""
    try:
        root = await asyncio.to_thread(git_ops.repo_root, body.repo)
        stacks = await asyncio.to_thread(detect.scan, root)
        d = await asyncio.to_thread(git_ops.diff, root, body.source, body.target)
    except git_ops.GitError as e:
        _err(e)
    types, groups = detect.for_change(stacks, git_ops.changed_paths(d["files"]))
    return {
        "types": types, "label": detect.label(types) if types else "",
        "files": len(git_ops.changed_paths(d["files"])),
        "counts": {REVIEWS[t]["label"]: len(groups.get(t, [])) for t in types},
    }


@app.post("/api/review/code")
async def review_code(body: CodeReviewIn):
    try:
        root = await asyncio.to_thread(git_ops.repo_root, body.repo)
        info = await asyncio.to_thread(git_ops.repo_info, root)
        stacks = await asyncio.to_thread(detect.scan, root)
        d = await asyncio.to_thread(git_ops.diff, root, body.source, body.target)
    except git_ops.GitError as e:
        _err(e)
    types, groups = detect.for_change(stacks, git_ops.changed_paths(d["files"]))
    if not types:
        _err(ValueError("ReviewKit couldn't find a .NET, Angular or Ionic project in this repository, "
                        "so it doesn't know which review rules to use."))
    label = detect.label(types)
    meta = {
        "kind": "code", "review": "+".join(types), "label": label,
        "title": f"{info['name']}: {body.source} → {body.target}",
    }
    gen = claude_runner.run(types, _code_prompt(info, d, body, types, groups), cwd=root, code=True)

    async def with_info():
        yield {"t": "info", "label": label, "counts": {REVIEWS[t]["label"]: len(groups.get(t, [])) for t in types}}
        async for ev in gen:
            yield ev

    return _ndjson(with_info(), meta)


@app.post("/api/review/td")
async def review_td(file: UploadFile = File(...), notes: str = Form("")):
    name = file.filename or "document.docx"
    if not name.lower().endswith(".docx"):
        _err(ValueError("Please upload a .docx file."))
    data = await file.read()
    try:
        text = await asyncio.to_thread(docx_to_text, data)
    except Exception as e:  # noqa: BLE001
        _err(ValueError(f"Could not read {name}: {e}"))
    prompt = f"Review this Technical Design Document: **{name}**\n\n<document>\n{text}\n</document>"
    if notes.strip():
        prompt += f"\n\n## Notes from the developer\n{notes.strip()}"
    prompt += POINTS_INSTRUCTION
    meta = {"kind": "td", "review": "td", "label": REVIEWS["td"]["label"], "title": name}

    def on_done(ev):
        ev["markdown"], points = extract_points(ev["markdown"])
        return {"meta": {"points": points}, "files": {"source.docx": data}}

    return _ndjson(claude_runner.run("td", prompt, code=False), meta, on_done)


class ApplyIn(BaseModel):
    selected: list[str]


def _disposition_md(points: list[dict]) -> str:
    label = {"applied": "Applied", "not_applicable": "Not applicable", "needs_input": "Needs author input",
             "failed": "Not applied (edit failed)"}
    rows = ["| Point | Severity | Title | Status | Notes |", "| --- | --- | --- | --- | --- |"]
    for p in points:
        note = (p.get("note") or "").replace("|", "/").replace("\n", " ")
        title = p.get("title", "").replace("|", "/")
        rows.append(f"| {p['id']} | {p.get('severity', '')} | {title} | {label.get(p.get('status'), p.get('status', ''))} | {note} |")
    return "## Review point disposition\n\n" + "\n".join(rows)


@app.post("/api/td/{entry_id}/apply")
async def apply_td(entry_id: str, body: ApplyIn):
    entry = history.get(entry_id)
    src = history.file_path(entry_id, "source.docx")
    if not entry or entry.get("kind") != "td" or not src.exists():
        raise HTTPException(404, "The original document for this review is no longer available.")
    points = entry.get("points") or []
    by_id = {p["id"]: p for p in points}
    selected = [i for i in body.selected if i in by_id]
    if not selected:
        _err(ValueError("Select at least one review point to apply."))

    async def events():
        data = src.read_bytes()
        try:
            yield {"t": "status", "v": f"Planning edits for {len(selected)} review point(s)…"}
            outline = await asyncio.to_thread(docx_editor.outline, data)
            styles = await asyncio.to_thread(docx_editor.used_styles, data)
            plan = await claude_runner.plan_edits(entry["title"], outline, styles, [by_id[i] for i in selected])

            # Hard guard: only edits tied to a point the user ticked are written.
            allowed = set(selected)
            edits = [e for e in plan.get("edits", []) if e.get("point_id") in allowed]
            dropped = len(plan.get("edits", [])) - len(edits)
            yield {"t": "status", "v": f"Writing {len(edits)} tracked change(s) into the document…"}
            new_bytes, results = await asyncio.to_thread(docx_editor.apply_edits, data, edits)
        except Exception as e:  # noqa: BLE001
            yield {"t": "error", "v": str(e)}
            return

        skipped = {s.get("point_id"): s.get("reason", "") for s in plan.get("skipped", [])}
        changes: dict[str, list[str]] = {}
        failures: dict[str, list[str]] = {}
        for r in results:
            (changes if r["ok"] else failures).setdefault(r["point_id"], []).append(
                r.get("summary", "") if r["ok"] else r.get("error", ""))
        for p in points:
            if p["id"] not in allowed:
                p["status"], p["note"] = "not_applicable", "Marked not applicable by the author."
            elif changes.get(p["id"]):
                p["status"], p["note"] = "applied", "; ".join(c for c in changes[p["id"]] if c)
            elif p["id"] in skipped:
                p["status"], p["note"] = "needs_input", skipped[p["id"]]
            else:
                p["status"] = "failed"
                p["note"] = "; ".join(failures.get(p["id"], [])) or "No edit was proposed for this point."

        stem = entry["title"][:-5] if entry["title"].lower().endswith(".docx") else entry["title"]
        out_name = f"{stem} (reviewed).docx"
        history.file_path(entry_id, "reviewed.docx").write_bytes(new_bytes)
        md = entry["markdown"].split("\n## Review point disposition")[0].rstrip()
        md += "\n\n" + _disposition_md(points)
        updated = history.update(entry_id, {"points": points, "reviewed_name": out_name}, md)
        yield {"t": "done", "points": points, "entry": updated, "markdown": md,
               "file": out_name, "dropped": dropped}

    return _stream_events(events())


@app.post("/api/td/{entry_id}/open")
async def open_reviewed(entry_id: str):
    p = history.file_path(entry_id, "reviewed.docx")
    if not p.exists():
        raise HTTPException(404, "No edited document yet.")
    os.startfile(str(p))  # opens in Word
    return {"ok": True}


@app.post("/api/td/{entry_id}/save")
async def save_reviewed(entry_id: str):
    e = history.get(entry_id)
    p = history.file_path(entry_id, "reviewed.docx")
    if not e or not p.exists():
        raise HTTPException(404, "No edited document yet.")
    win = WINDOW["ref"]
    if win is None:
        raise HTTPException(501, "Save is only available in the desktop window; use Open instead.")
    import webview

    result = await asyncio.to_thread(
        win.create_file_dialog, webview.FileDialog.SAVE,
        save_filename=e.get("reviewed_name") or "reviewed.docx", file_types=("Word document (*.docx)",),
    )
    if not result:
        return {"ok": False}
    dest = result if isinstance(result, str) else result[0]
    shutil.copyfile(p, dest)
    return {"ok": True, "path": dest}


# ---------- history ----------

@app.get("/api/history")
async def list_history():
    await asyncio.to_thread(_purge_history)
    items = history.list_all()
    for e in items:  # show current labels (e.g. ".NET") for older entries too
        if e.get("review") in REVIEWS:
            e["label"] = REVIEWS[e["review"]]["label"]
    return items


@app.get("/api/history/{entry_id}")
async def get_history(entry_id: str):
    e = history.get(entry_id)
    if not e:
        raise HTTPException(404, "Not found")
    if e.get("review") in REVIEWS:
        e["label"] = REVIEWS[e["review"]]["label"]
    return e


@app.delete("/api/history/{entry_id}")
async def delete_history(entry_id: str):
    history.delete(entry_id)
    return {"ok": True}


@app.post("/api/history/{entry_id}/export")
async def export_history(entry_id: str):
    e = history.get(entry_id)
    if not e:
        raise HTTPException(404, "Not found")
    win = WINDOW["ref"]
    if win is None:
        raise HTTPException(501, "Export is only available in the desktop window; use Copy instead.")
    import webview

    safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in e["title"])[:80] or "review"
    result = await asyncio.to_thread(
        win.create_file_dialog, webview.FileDialog.SAVE,
        save_filename=f"{safe}.md", file_types=("Markdown (*.md)",),
    )
    if not result:
        return {"ok": False}
    path = result if isinstance(result, str) else result[0]
    Path(path).write_text(e["markdown"], encoding="utf-8")
    return {"ok": True, "path": path}


# ---------- UI ----------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
