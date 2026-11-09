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
        if p.get("file"):  # code reviews
            points[-1]["file"] = str(p["file"])
            points[-1]["line"] = str(p.get("line") or "")
    return clean, points


CODE_POINTS_INSTRUCTION = """

After the review, append exactly one block in this format. It is hidden from the reader and lets the developer choose review points for ReviewKit to apply to the code:
<!-- REVIEW_POINTS
[{"id": "CP1", "severity": "Critical|High|Medium|Low", "file": "<repo-relative path of the file to change>", "line": "<line number or range, or empty>", "title": "<short title>", "change": "<the concrete code change that resolves it>"}]
-->
Include every actionable finding from your review (issues, suggestions and line comments that call for a code change), most severe first. Leave out praise and points that need no code change. Do not mention this block in the review text."""


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
    return "\n\n".join(parts) + CODE_POINTS_INSTRUCTION


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
        "repo": root, "source": body.source.strip().removeprefix("origin/"), "target": body.target.strip(),
        "source_sha": d["source_sha"], "types": types,
    }

    def on_done(ev):
        ev["markdown"], points = extract_points(ev["markdown"])
        return {"meta": {"points": points}}

    gen = claude_runner.run(types, _code_prompt(info, d, body, types, groups), cwd=root, code=True)

    async def with_info():
        yield {"t": "info", "label": label, "counts": {REVIEWS[t]["label"]: len(groups.get(t, [])) for t in types}}
        async for ev in gen:
            yield ev

    return _ndjson(with_info(), meta, on_done)


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


# ---------- apply code review points (working tree only; never commits or pushes) ----------

def _sha_file(path: Path) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _undo_state(entry: dict) -> tuple[bool, str]:
    """Can the last apply be reverted exactly? Only if nothing changed since (HEAD and file contents)."""
    a = entry.get("applied")
    if not a:
        return False, "Nothing to undo."
    root = Path(entry["repo"])
    try:
        if git_ops.rev(str(root), "HEAD") != a["head"]:
            return False, "There are new commits on this branch since ReviewKit's changes, so they can't be undone automatically."
    except git_ops.GitError as e:
        return False, str(e)
    for rel, h in a.get("hashes", {}).items():
        if _sha_file(root / rel) != h:
            return False, f"{rel} was changed after ReviewKit edited it, so the changes can't be undone automatically."
    return True, ""


def _code_check(entry: dict) -> dict:
    """Pre-flight checks before applying points to the developer's clone."""
    root, source = entry.get("repo", ""), entry.get("source", "")
    out = {"ok": False, "error": "", "warning": "", "repo": root, "source": source, "branch": "", "previous": False}
    if not root or not Path(root).is_dir():
        out["error"] = f"The repository folder {root or '(unknown)'} is not available on this PC."
        return out
    try:
        branch = git_ops.current_branch(root)
        out["branch"] = branch
        if branch != source:
            out["error"] = (f"Check out {source} in {Path(root).name} first. Your clone is on "
                            f"{branch or 'a detached HEAD'}, and ReviewKit only changes files on the branch that was reviewed.")
            return out
        head = git_ops.rev(root, "HEAD")
        sha = entry.get("source_sha", "")
        if sha and not git_ops.is_ancestor(root, sha, head):
            out["error"] = (f"Your local {source} doesn't contain the commit that was reviewed ({sha[:7]}). "
                            "Pull the branch first, then apply.")
            return out
        if sha and head != sha:
            out["warning"] = f"{source} has newer commits than the reviewed one ({sha[:7]}). Points are applied to the current code."
        undoable, _ = _undo_state(entry)
        out["previous"] = bool(entry.get("applied"))
        ours = set(entry["applied"].get("files", [])) if undoable else set()
        dirty = [f for f in git_ops.dirty_tracked(root) if f not in ours]
        if entry.get("applied") and not undoable:
            out["error"] = ("ReviewKit's earlier changes were edited or committed since, so it can't replace them. "
                            "Commit or discard them first, then apply again.")
            return out
        if dirty:
            out["error"] = (f"You have uncommitted changes in {len(dirty)} file(s) (e.g. {dirty[0]}). Commit or stash them "
                            "first, so ReviewKit's changes stay separate and can be undone.")
            return out
    except git_ops.GitError as e:
        out["error"] = str(e)
        return out
    out["ok"] = True
    return out


def _code_undo(entry: dict) -> None:
    a = entry["applied"]
    root = Path(entry["repo"])
    git_ops.restore(str(root), a.get("files", []))
    for rel in a.get("new_files", []):
        (root / rel).unlink(missing_ok=True)


@app.get("/api/code/{entry_id}/check")
async def code_check(entry_id: str):
    entry = history.get(entry_id)
    if not entry or entry.get("kind") != "code":
        raise HTTPException(404, "Review not found.")
    return await asyncio.to_thread(_code_check, entry)


@app.post("/api/code/{entry_id}/apply")
async def apply_code(entry_id: str, body: ApplyIn):
    entry = history.get(entry_id)
    if not entry or entry.get("kind") != "code" or not entry.get("points"):
        raise HTTPException(404, "This review has no review points to apply.")
    points = entry["points"]
    by_id = {p["id"]: p for p in points}
    selected = [i for i in body.selected if i in by_id]
    if not selected:
        _err(ValueError("Select at least one review point to apply."))

    async def events():
        root = entry["repo"]
        try:
            check = await asyncio.to_thread(_code_check, entry)
            if not check["ok"]:
                raise RuntimeError(check["error"])
            if entry.get("applied"):
                yield {"t": "status", "v": "Reverting ReviewKit's previous changes…"}
                await asyncio.to_thread(_code_undo, entry)
            before = set(await asyncio.to_thread(git_ops.untracked, root))
            yield {"t": "status", "v": f"Applying {len(selected)} review point(s) to {entry['source']}…"}
            result = {"results": []}
            types = entry.get("types") or [t for t in entry.get("review", "").split("+") if t in REVIEWS] or ["capataz"]
            async for ev in claude_runner.apply_code_points(root, types, [by_id[i] for i in selected], entry["source"]):
                if ev["t"] == "tool":
                    yield {"t": "status", "v": ev["v"]}
                elif ev["t"] == "result":
                    result = ev["data"]
            files = await asyncio.to_thread(git_ops.modified_tracked, root)
            new_files = sorted(set(await asyncio.to_thread(git_ops.untracked, root)) - before)
            diff = await asyncio.to_thread(git_ops.working_diff, root, new_files)
            head = await asyncio.to_thread(git_ops.rev, root, "HEAD")
        except Exception as e:  # noqa: BLE001
            yield {"t": "error", "v": str(e)}
            return

        by_result = {r.get("point_id"): r for r in result.get("results", []) if isinstance(r, dict)}
        for p in points:
            r = by_result.get(p["id"])
            if p["id"] not in selected:
                p["status"], p["note"] = "not_applicable", "Marked not applicable by the developer."
            elif r and r.get("status") == "applied":
                p["status"], p["note"] = "applied", r.get("summary", "")
            elif r:
                p["status"], p["note"] = "needs_input", r.get("summary", "")
            else:
                p["status"], p["note"] = "failed", "Claude didn't report a change for this point."
        applied = None
        if files or new_files:
            applied = {"head": head, "files": files, "new_files": new_files,
                       "hashes": {f: _sha_file(Path(root) / f) for f in files + new_files}}
            history.file_path(entry_id, "changes.diff").write_text(diff, encoding="utf-8")
        md = entry["markdown"].split("\n## Review point disposition")[0].rstrip() + "\n\n" + _disposition_md(points)
        updated = history.update(entry_id, {"points": points, "applied": applied}, md)
        yield {"t": "done", "points": points, "entry": updated, "markdown": md}

    return _stream_events(events())


@app.get("/api/code/{entry_id}/changes")
async def code_changes(entry_id: str):
    p = history.file_path(entry_id, "changes.diff")
    return {"diff": p.read_text(encoding="utf-8") if p.exists() else ""}


@app.post("/api/code/{entry_id}/undo")
async def code_undo(entry_id: str):
    entry = history.get(entry_id)
    if not entry or entry.get("kind") != "code":
        raise HTTPException(404, "Review not found.")
    ok, reason = await asyncio.to_thread(_undo_state, entry)
    if not ok:
        _err(ValueError(reason))
    await asyncio.to_thread(_code_undo, entry)
    points = entry.get("points") or []
    for p in points:
        p["status"] = "open"
        p.pop("note", None)
    md = entry["markdown"].split("\n## Review point disposition")[0].rstrip()
    history.file_path(entry_id, "changes.diff").unlink(missing_ok=True)
    return {"entry": history.update(entry_id, {"points": points, "applied": None}, md), "markdown": md}


@app.post("/api/code/{entry_id}/open-folder")
async def code_open_folder(entry_id: str):
    entry = history.get(entry_id)
    if not entry or not Path(entry.get("repo", "")).is_dir():
        raise HTTPException(404, "The repository folder is not available.")
    os.startfile(entry["repo"])
    return {"ok": True}


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


@app.delete("/api/history")
async def clear_history():
    return {"ok": True, "deleted": await asyncio.to_thread(history.clear)}


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
