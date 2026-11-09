"""HTTP API used by the ReviewKit window (served on 127.0.0.1 only)."""
import asyncio
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import claude_runner, git_ops, history, settings
from .docx_reader import docx_to_text
from .paths import STATIC_DIR
from .skills import REVIEWS

VERSION = "1.0.0"
app = FastAPI(title="ReviewKit", docs_url=None, redoc_url=None)

# Set by main.py when running inside a pywebview window (for native dialogs).
WINDOW = {"ref": None}


class RepoIn(BaseModel):
    path: str


class CodeReviewIn(BaseModel):
    repo: str
    source: str
    target: str
    review: str
    notes: str = ""


class SettingsIn(BaseModel):
    api_key: str | None = None
    model: str | None = None
    theme: str | None = None


def _err(e: Exception, code: int = 400):
    raise HTTPException(status_code=code, detail=str(e))


# ---------- connection / settings ----------

@app.get("/api/status")
async def status():
    auth = await asyncio.to_thread(claude_runner.auth_status)
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


@app.post("/api/logout")
async def logout():
    await asyncio.to_thread(claude_runner.logout)
    return {"ok": True}


@app.post("/api/test")
async def test():
    return await claude_runner.test_connection()


def _public_settings(s: dict) -> dict:
    key = s.get("api_key", "")
    return {**s, "api_key": "", "api_key_set": bool(key), "api_key_hint": f"…{key[-4:]}" if key else ""}


@app.get("/api/settings")
async def get_settings():
    return _public_settings(settings.load())


@app.post("/api/settings")
async def set_settings(body: SettingsIn):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    return _public_settings(settings.save(values))


# ---------- repos ----------

@app.post("/api/repo")
async def open_repo(body: RepoIn):
    try:
        root = await asyncio.to_thread(git_ops.repo_root, body.path.strip().strip('"'))
        info = await asyncio.to_thread(git_ops.repo_info, root)
        info["branches"] = await asyncio.to_thread(git_ops.branches, root)
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

def _ndjson(gen, meta: dict):
    async def stream():
        async for ev in gen:
            if ev["t"] == "done" and ev.get("markdown"):
                ev["entry"] = history.add({**meta, "seconds": ev.get("seconds")}, ev["markdown"])
            yield json.dumps(ev) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _code_prompt(info: dict, d: dict, body: CodeReviewIn) -> str:
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
    if body.notes.strip():
        parts.append(f"## Notes from the developer\n{body.notes.strip()}")
    return "\n\n".join(parts)


@app.post("/api/review/code")
async def review_code(body: CodeReviewIn):
    if body.review not in REVIEWS or REVIEWS[body.review]["kind"] != "code":
        _err(ValueError("Unknown review type."))
    try:
        root = await asyncio.to_thread(git_ops.repo_root, body.repo)
        info = await asyncio.to_thread(git_ops.repo_info, root)
        d = await asyncio.to_thread(git_ops.diff, root, body.source, body.target)
    except git_ops.GitError as e:
        _err(e)
    meta = {
        "kind": "code", "review": body.review, "label": REVIEWS[body.review]["label"],
        "title": f"{info['name']}: {body.source} → {body.target}",
    }
    gen = claude_runner.run(body.review, _code_prompt(info, d, body), cwd=root, code=True)
    return _ndjson(gen, meta)


@app.post("/api/review/td")
async def review_td(file: UploadFile = File(...), notes: str = Form("")):
    name = file.filename or "document.docx"
    if not name.lower().endswith(".docx"):
        _err(ValueError("Please upload a .docx file."))
    try:
        text = await asyncio.to_thread(docx_to_text, await file.read())
    except Exception as e:  # noqa: BLE001
        _err(ValueError(f"Could not read {name}: {e}"))
    prompt = f"Review this Technical Design Document: **{name}**\n\n<document>\n{text}\n</document>"
    if notes.strip():
        prompt += f"\n\n## Notes from the developer\n{notes.strip()}"
    meta = {"kind": "td", "review": "td", "label": REVIEWS["td"]["label"], "title": name}
    return _ndjson(claude_runner.run("td", prompt, code=False), meta)


# ---------- history ----------

@app.get("/api/history")
async def list_history():
    return history.list_all()


@app.get("/api/history/{entry_id}")
async def get_history(entry_id: str):
    e = history.get(entry_id)
    if not e:
        raise HTTPException(404, "Not found")
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
