"""HTTP API used by the ReviewKit window (served on 127.0.0.1 only)."""
import asyncio
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (activity, azure_devops, claude_runner, detect, docx_editor, git_ops, history, review_request, settings,
               updater, versions)
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
    work_email: str | None = None


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
    act = await asyncio.to_thread(activity.sync, auth, VERSION)
    return {
        "version": VERSION,
        "git": shutil.which("git") is not None,
        "auth": auth,
        "activity": act,
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
    activity.log("claude.sign_out")
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
    if "work_email" in values:
        values["work_email"] = values["work_email"].strip().lower()
        if values["work_email"] and not re.fullmatch(r"[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+", values["work_email"]):
            _err(ValueError("Enter a valid work e-mail address."))
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
    if "work_email" in values or "api_key" in values:
        await asyncio.to_thread(activity.sync, await asyncio.to_thread(claude_runner.auth_status), VERSION)
    changed = sorted(k for k in values if k != "theme")
    if changed:
        activity.log("settings.change", keys=changed)
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
    activity.log("app.update", from_version=VERSION)

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
        stacks = await asyncio.to_thread(detect.scan, root)
        info["stack"] = detect.describe(stacks)
    except git_ops.GitError as e:
        _err(e)
    info["stack"].update(await asyncio.to_thread(_stack_versions, root, stacks))
    settings.remember_repo(root)
    return info


def _stack_versions(root: str, stacks: dict) -> dict:
    """Versions shown under the review type, read from the checked-out commit (and remembered)."""
    try:
        rows = versions.summary(versions.for_commit(root, "HEAD"), stacks)
    except Exception:  # noqa: BLE001 - showing versions is optional
        return {"versions": []}
    for r in rows:
        r["label"] = REVIEWS[r["type"]]["label"]
    return {"versions": rows, "versions_branch": git_ops.current_branch(root) or "the checked-out commit"}


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
                _log_review(entry)
            yield json.dumps(ev) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _log_review(entry: dict) -> None:
    points = entry.get("points") or []
    common = {"points": len(points), "seconds": entry.get("seconds"),
              "critical": sum(1 for p in points if (p.get("severity") or "").lower() == "critical") or None}
    if entry.get("kind") == "code":
        activity.log("code_review.run", repo=_repo_name(entry), source=entry.get("source"),
                     target=entry.get("target"), review=entry.get("review"), **common)
    else:
        activity.log("td_review.run", document=entry.get("title"), **common)


def _status_counts(points: list[dict], selected: list[str]) -> dict:
    done = [p for p in points if p.get("id") in selected]
    return {"selected": len(selected), "applied": sum(1 for p in done if p.get("status") == "applied"),
            "needs_input": sum(1 for p in done if p.get("status") == "needs_input"),
            "failed": sum(1 for p in done if p.get("status") == "failed")}


def _repo_name(entry: dict) -> str:
    return entry.get("repo_name") or Path(entry.get("repo", "")).name


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
Include every actionable finding from your review (issues, suggestions and line comments that call for a code change), most severe first. Leave out praise and points that need no code change.

Then append one more hidden block, used for the pull request this change will be submitted as:
<!-- PR_SUMMARY
{"summary": "<what the change does, as a short sentence-case phrase of 4-12 words, e.g. Fill the company location from the map pin; no ticket number, no type prefix, no trailing period>"}
-->
Do not mention either block in the review text."""

_SUMMARY_RE = re.compile(r"<!--\s*PR_SUMMARY\s*(.*?)-->", re.S)


def extract_pr_summary(markdown: str) -> tuple[str, dict]:
    """Split the hidden PR_SUMMARY block off the review; returns (markdown, {"pr_summary"})."""
    m = _SUMMARY_RE.search(markdown)
    if not m:
        return markdown, {}
    clean = (markdown[: m.start()] + markdown[m.end():]).strip()
    try:
        raw = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return clean, {}
    if not isinstance(raw, dict):
        return clean, {}
    return clean, {"pr_summary": str(raw.get("summary", "")).strip()}


def _versions_section(root: str, d: dict, types: list[str]) -> str:
    """Known framework/library versions for the prompt ('' if they can't be read; the review still runs)."""
    try:
        found = versions.for_commit(root, d["source_sha"])
        return versions.prompt_section(found, types, git_ops.changed_paths(d["files"]))
    except Exception:
        return ""


def _code_prompt(info: dict, d: dict, body: CodeReviewIn, types: list[str], groups: dict, known: str = "") -> str:
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
    if known:
        parts.insert(3, known)
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
        _err(ValueError("ReviewKit couldn't find a .NET, Python, Angular, Ionic, React or React Native project in this repository, "
                        "so it doesn't know which review rules to use."))
    label = detect.label(types)
    meta = {
        "kind": "code", "review": "+".join(types), "label": label,
        "title": f"{info['name']}: {body.source} → {body.target}",
        "repo": root, "repo_name": info["name"],
        "source": body.source.strip().removeprefix("origin/"), "target": body.target.strip(),
        "source_sha": d["source_sha"], "types": types,
    }

    def on_done(ev):
        md, pr = extract_pr_summary(ev["markdown"])
        ev["markdown"], points = extract_points(md)
        return {"meta": {"points": points, **pr}}

    known = await asyncio.to_thread(_versions_section, root, d, types)
    gen = claude_runner.run(types, _code_prompt(info, d, body, types, groups, known), cwd=root, code=True)

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
        activity.log("td_review.apply", document=entry["title"], **_status_counts(points, selected))
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
        activity.log("code_review.apply", repo=_repo_name(entry), source=entry.get("source"),
                     files=len(files) + len(new_files), **_status_counts(points, selected))
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
    activity.log("code_review.undo", repo=_repo_name(entry), source=entry.get("source"))
    return {"entry": history.update(entry_id, {"points": points, "applied": None}, md), "markdown": md}


@app.post("/api/code/{entry_id}/open-folder")
async def code_open_folder(entry_id: str):
    entry = history.get(entry_id)
    if not entry or not Path(entry.get("repo", "")).is_dir():
        raise HTTPException(404, "The repository folder is not available.")
    os.startfile(entry["repo"])
    return {"ok": True}


# ---------- pull request + review request email ----------

class PrIn(BaseModel):
    title: str
    description: str = ""
    to: str
    greeting: str = ""
    reviewers: str = ""  # required reviewers, free text: emails or names separated by ';'


class OpenIn(BaseModel):
    what: str  # "pr" | "email"


def _code_entry(entry_id: str) -> dict:
    entry = history.get(entry_id)
    if not entry or entry.get("kind") != "code":
        raise HTTPException(404, "Review not found.")
    if not Path(entry.get("repo", "")).is_dir():
        _err(ValueError("The repository folder for this review isn't available any more."))
    return entry


def _branches(entry: dict) -> tuple[str, str]:
    return entry["source"].strip().removeprefix("origin/"), entry["target"].strip().removeprefix("origin/")


def _pr_prepare(entry: dict) -> dict:
    root = entry["repo"]
    source, target = _branches(entry)
    repo = azure_devops.from_repo(root)
    head = git_ops.remote_sha(root, source)
    if not head:
        raise azure_devops.AzureError(f"{source} isn't on Azure DevOps yet (or ReviewKit hasn't fetched it). "
                                      "Push the branch, click Fetch, then try again.")
    if not git_ops.remote_sha(root, target):
        raise azure_devops.AzureError(f"The target branch {target} isn't on Azure DevOps.")
    note = ""
    if entry.get("source_sha") and head != entry["source_sha"]:
        n = git_ops.count_commits(root, entry["source_sha"], head)
        note = (f"{source} has {n} new commit{'s' if n != 1 else ''} since this review. "
                if n else f"{source} has changed since this review. ") + "The pull request includes the branch as it is now."
    s = settings.load()
    return {
        "repo_name": repo.name, "source": source, "target": target,
        "title": review_request.pr_title(source, entry.get("pr_summary", "")),
        "description": review_request.pr_description(git_ops.commit_subjects(root, target, source)),
        "to": s.get("pr_to", ""), "greeting": s.get("pr_greeting", ""), "reviewers": s.get("pr_reviewers", ""),
        "people": review_request.people(s.get("pr_people", [])),
        "existing": azure_devops.find_pr(repo, source, target),
        "note": note,
    }


@app.get("/api/code/{entry_id}/pr")
async def pr_prepare(entry_id: str):
    entry = _code_entry(entry_id)
    try:
        return await asyncio.to_thread(_pr_prepare, entry)
    except (azure_devops.AzureError, git_ops.GitError) as e:
        _err(e)


def _pr_create(entry_id: str, entry: dict, body: PrIn) -> dict:
    root = entry["repo"]
    source, target = _branches(entry)
    to, greeting, wanted = body.to.strip(), body.greeting.strip(), body.reviewers.strip()
    # Remembered for the next pull request in any repository, even if this one fails below.
    settings.save({"pr_to": to, "pr_greeting": greeting, "pr_reviewers": wanted})
    repo = azure_devops.from_repo(root)
    reviewers = azure_devops.resolve_reviewers(repo, review_request.split_list(wanted))  # before creating anything
    pr = azure_devops.find_pr(repo, source, target)  # never create a second PR for the same branches
    existing = bool(pr)
    if pr:
        azure_devops.require_reviewers(repo, pr["id"], reviewers)
    else:
        pr = azure_devops.create_pr(repo, source, target, body.title.strip(), body.description.strip(), reviewers)
    record = {**pr, "existing": existing, "to": to, "greeting": greeting,
              "reviewers": [r["name"] for r in reviewers],
              "created": time.strftime("%Y-%m-%d %H:%M"), "repo_name": repo.name}
    updated = history.update(entry_id, {"pr": record})
    activity.log("pr.create", repo=repo.name, pr_id=pr.get("id"), existing=existing, source=source, target=target,
                 reviewers=len(reviewers))
    os.startfile(review_request.compose_url(to, pr, greeting))
    return {"pr": record, "entry": updated}


@app.post("/api/code/{entry_id}/pr")
async def pr_create(entry_id: str, body: PrIn):
    entry = _code_entry(entry_id)
    if not body.title.strip():
        _err(ValueError("Enter a pull request title."))
    if not review_request.split_list(body.to):
        _err(ValueError("Enter who the email goes to."))
    bad = review_request.bad_addresses(body.to)
    if bad:
        _err(ValueError(f"“{bad[0]}” isn't an email address. Separate several addresses with ;"))
    try:
        return await asyncio.to_thread(_pr_create, entry_id, entry, body)
    except (azure_devops.AzureError, git_ops.GitError) as e:
        _err(e)


class PersonIn(BaseModel):
    email: str
    name: str = ""


@app.post("/api/pr/people")
async def add_person(body: PersonIn):
    """Add someone with "Other…": kept on this PC and listed in the dropdowns from now on."""
    email, name = body.email.strip(), body.name.strip()
    if not review_request.valid_email(email):
        _err(ValueError(f"“{email}” isn't an email address."))
    custom = [p for p in settings.load().get("pr_people", []) if p.get("email", "").lower() != email.lower()]
    if email.lower() not in {p["email"].lower() for p in review_request.PEOPLE}:
        custom.append({"email": email, "name": name})
    settings.save({"pr_people": custom})
    return {"people": review_request.people(custom)}


@app.post("/api/pr/people/remove")
async def remove_person(body: PersonIn):
    custom = [p for p in settings.load().get("pr_people", []) if p.get("email", "").lower() != body.email.strip().lower()]
    settings.save({"pr_people": custom})
    return {"people": review_request.people(custom)}


@app.post("/api/code/{entry_id}/pr/open")
async def pr_open(entry_id: str, body: OpenIn):
    entry = history.get(entry_id)
    pr = (entry or {}).get("pr")
    if not pr:
        raise HTTPException(404, "No pull request for this review yet.")
    url = pr["url"] if body.what == "pr" else review_request.compose_url(pr.get("to", ""), pr, pr.get("greeting", ""))
    os.startfile(url)
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
    activity.log("td_review.save_document", document=e.get("title"))
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
    deleted = await asyncio.to_thread(history.clear)
    activity.log("history.clear", deleted=deleted)
    return {"ok": True, "deleted": deleted}


@app.delete("/api/history/{entry_id}")
async def delete_history(entry_id: str):
    e = history.get(entry_id)
    history.delete(entry_id)
    if e:
        activity.log("history.delete", title=e.get("title"), kind=e.get("kind"))
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
    activity.log("history.export", title=e.get("title"), kind=e.get("kind"))
    return {"ok": True, "path": path}


# ---------- users and activity (admin page) ----------

class AdminSignInIn(BaseModel):
    email: str
    password: str = ""


class AdminActiveIn(BaseModel):
    email: str
    active: bool


async def _admin_call(fn, *args):
    if not activity.ENABLED:
        raise HTTPException(404, "This ReviewKit build isn't connected to Supabase.")
    try:
        return await asyncio.to_thread(fn, *args)
    except activity.AdminAuthError as e:
        raise HTTPException(401, str(e)) from None
    except activity.SupabaseError as e:
        raise HTTPException(502, f"Supabase: {e}") from None
    except OSError as e:
        raise HTTPException(503, f"Couldn't reach Supabase: {e}") from None


@app.get("/api/admin/session")
async def admin_session():
    return activity.admin_status()


@app.post("/api/admin/sign-in")
async def admin_sign_in(body: AdminSignInIn):
    if not body.email.strip() or not body.password:
        _err(ValueError("Enter the admin e-mail and password."))
    return await _admin_call(activity.admin_sign_in, body.email, body.password)


@app.post("/api/admin/sign-out")
async def admin_sign_out():
    await asyncio.to_thread(activity.admin_sign_out)
    return {"ok": True}


@app.get("/api/admin/users")
async def admin_users():
    return await _admin_call(activity.admin_users)


@app.get("/api/admin/logs")
async def admin_logs(email: str = "", action: str = "", before: int | None = None):
    return await _admin_call(activity.admin_logs, email, action, before)


@app.post("/api/admin/users/active")
async def admin_set_active(body: AdminActiveIn):
    return await _admin_call(activity.admin_set_active, body.email, body.active)


# Starting a review, applying points or creating a PR needs a user who hasn't been turned off.
_GUARDED = re.compile(r"^/api/(review/(code|td)|code/[^/]+/(apply|pr)|td/[^/]+/apply)$")


@app.middleware("http")
async def require_access(request, call_next):
    if activity.ENABLED and request.method == "POST" and _GUARDED.match(request.url.path):
        await asyncio.to_thread(activity.ensure_synced)
        reason = activity.access_error()
        if reason:
            return JSONResponse({"detail": reason}, status_code=403)
    return await call_next(request)


# ---------- UI ----------

@app.middleware("http")
async def no_stale_ui(request, call_next):
    """Make the window re-check the UI files on every load (cheap: they come from this PC).

    Without this the embedded browser may keep an older app.js after an update, which breaks
    the page when it no longer matches the new index.html."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
