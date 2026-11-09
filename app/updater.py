"""Self-update: checks the download page for a newer build and installs it in place.

CI publishes version.json next to ReviewKit.zip on GitHub Pages. Installing downloads the zip,
verifies its SHA-256, extracts it under %APPDATA%\\ReviewKit\\update, and hands off to a small
PowerShell script that waits for this process to exit, copies the new files over the install
folder and starts the new version.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterator

from .paths import data_dir

UPDATE_BASE = os.environ.get("REVIEWKIT_UPDATE_URL", "https://denamielleonardo.github.io/reviewkit/").rstrip("/") + "/"
_HEADERS = {"User-Agent": "ReviewKit-updater", "Cache-Control": "no-cache"}
_WORK = data_dir() / "update"


def _parse(v: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except ValueError:
        return None


def install_dir() -> Path | None:
    """Folder holding ReviewKit.exe, when running as the packaged app."""
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return None
    return Path(sys.executable).resolve().parent


def _writable(folder: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".rk-write-test-"):
            pass
        return True
    except OSError:
        return False


def _fetch_manifest() -> dict:
    req = urllib.request.Request(f"{UPDATE_BASE}version.json?t={int(time.time())}", headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    if not isinstance(data, dict) or not data.get("version"):
        raise RuntimeError("The update server returned an invalid version.json.")
    return data


def check(current: str) -> dict:
    info = _fetch_manifest()
    latest = str(info["version"])
    cur, new = _parse(current), _parse(latest)
    folder = install_dir()
    return {
        "current": current,
        "latest": latest,
        "date": info.get("date", ""),
        "available": bool(cur and new and new > cur),
        # Dev runs (python -m app.main) can see updates but not install them.
        "can_install": bool(folder and _writable(folder)),
        "reason": "" if folder else "Updates install only in the packaged app.",
    }


def _ps_quote(p: Path | str) -> str:
    return "'" + str(p).replace("'", "''") + "'"


def install() -> Iterator[dict]:
    """Download, verify and stage the latest build, then start the hand-off script.

    Yields progress events; the caller must exit the app after the final "done" event.
    """
    folder = install_dir()
    if not folder:
        raise RuntimeError("Updates install only in the packaged app.")
    if not _writable(folder):
        raise RuntimeError(f"ReviewKit can't write to {folder}. Move the ReviewKit folder somewhere you own "
                           "(e.g. Documents) or download the update manually.")

    yield {"t": "status", "v": "Checking the latest version…"}
    info = _fetch_manifest()
    url = UPDATE_BASE + str(info.get("file") or "ReviewKit.zip")
    expected = str(info.get("sha256", "")).lower()

    shutil.rmtree(_WORK, ignore_errors=True)
    _WORK.mkdir(parents=True, exist_ok=True)
    zpath = _WORK / "ReviewKit.zip"

    req = urllib.request.Request(url, headers=_HEADERS)
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(zpath, "wb") as out:
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        done, last = 0, -1
        while chunk := r.read(1 << 16):
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else 0
            if pct != last:
                last = pct
                yield {"t": "progress", "v": pct, "done": done, "total": total}
    if expected and h.hexdigest() != expected:
        raise RuntimeError("The downloaded update failed its integrity check. Try again later.")

    yield {"t": "status", "v": "Unpacking…"}
    staging = _WORK / "staging"
    with zipfile.ZipFile(zpath) as z:
        z.extractall(staging)
    exe = next(staging.rglob("ReviewKit.exe"), None)
    if not exe:
        raise RuntimeError("The update package doesn't contain ReviewKit.exe.")
    src = exe.parent

    log = _WORK / "update.log"
    script = _WORK / "apply-update.ps1"
    script.write_text(f"""
$log = {_ps_quote(log)}
function Log($m) {{ "$(Get-Date -Format s) $m" | Out-File -FilePath $log -Append -Encoding utf8 }}
Log 'Waiting for ReviewKit to close'
Wait-Process -Id {os.getpid()} -Timeout 60 -ErrorAction SilentlyContinue
$dst = {_ps_quote(folder)}
# The bundled Claude CLI may still be running from the old folder (e.g. an open sign-in console).
Get-Process -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -and $_.Path.StartsWith($dst, 'OrdinalIgnoreCase') }} |
  Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800
robocopy {_ps_quote(src / '_internal')} {_ps_quote(folder / '_internal')} /MIR /R:10 /W:1 /NP /NFL /NDL /NJH | Out-File -FilePath $log -Append -Encoding utf8
$a = $LASTEXITCODE
robocopy {_ps_quote(src)} $dst /R:10 /W:1 /NP /NFL /NDL /NJH | Out-File -FilePath $log -Append -Encoding utf8
$b = $LASTEXITCODE
if ($a -ge 8 -or $b -ge 8) {{ Log "Copy failed (robocopy $a / $b)" }} else {{ Log 'Update installed' }}
Start-Process -FilePath (Join-Path $dst 'ReviewKit.exe')
""", encoding="utf-8-sig")

    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True, cwd=str(_WORK),
    )
    yield {"t": "done", "v": str(info["version"])}
