"""Shared user list and activity log in Supabase.

Each user is identified by the e-mail of their Claude sign-in (or, with an API key, the work e-mail
they enter in Settings). The app uses only the project's public key and can only call two database
functions: rk_register (adds/refreshes the user, says whether they're allowed in) and rk_log (adds
log rows). It can't read anything back. See supabase/migrations/ for the tables and rules.

Only metadata is logged (what was done, repository/branch/document names, counts), never code,
diffs or review text. Events are written to a queue file first and sent in the background, so
ReviewKit works the same when offline or when Supabase is down; queued events go out later.

The admin page signs in to Supabase Auth with the admin user's e-mail and password. That session is kept in
memory only, so the admin signs in again after restarting ReviewKit.

The build sets SUPABASE_URL and SUPABASE_KEY in app/_build.py (REVIEWKIT_SUPABASE_URL and
REVIEWKIT_SUPABASE_KEY override them for testing). Without them all of this is switched off.
"""
import json
import os
import platform
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import net, settings
from .paths import data_dir

try:  # written by CI at build time
    from . import _build
except ImportError:
    _build = None

URL = (os.environ.get("REVIEWKIT_SUPABASE_URL") or getattr(_build, "SUPABASE_URL", "") or "").strip().rstrip("/")
KEY = (os.environ.get("REVIEWKIT_SUPABASE_KEY") or getattr(_build, "SUPABASE_KEY", "") or "").strip()
ENABLED = bool(URL and KEY)
MACHINE = (platform.node() or "")[:100]

REGISTER_EVERY = 10 * 60  # re-check "is this user still allowed?" this often (seconds)
MAX_QUEUE = 5000          # oldest events are dropped beyond this many unsent ones
BATCH = 100

_QUEUE = data_dir() / "activity-queue.jsonl"
_STATE = data_dir() / "activity.json"  # last answer from rk_register, used when offline

_lock = threading.Lock()
_wake = threading.Event()
_flusher: dict = {"thread": None}
_ctx = {"version": "dev", "email": "", "org": None, "method": None, "synced": False, "opened": False}
_registered: set[str] = set()


class SupabaseError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _request(method: str, path: str, body=None, token: str | None = None, timeout: float = 10,
             prefer: str | None = None):
    headers = {"apikey": KEY, "Accept": "application/json"}
    bearer = token or (KEY if KEY.startswith("eyJ") else None)  # legacy anon keys are JWTs; new publishable keys aren't
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if prefer:
        headers["Prefer"] = prefer
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(URL + path, data=data, headers=headers, method=method)
    try:
        with net.urlopen(req, timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            detail = {}
        msg = (detail.get("msg") or detail.get("message") or detail.get("error_description")
               or detail.get("error") or f"Supabase returned HTTP {e.code}")
        raise SupabaseError(e.code, str(msg)) from None
    return json.loads(raw) if raw else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- who is using the app ----------

def _load_state() -> dict:
    try:
        data = json.loads(_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(data: dict) -> None:
    try:
        _STATE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _identity(auth: dict) -> tuple[str, bool]:
    """(e-mail, needs_email). API-key users have no Claude e-mail, so they type their work e-mail."""
    if auth.get("authMethod") == "api_key":
        email = (settings.load().get("work_email") or "").strip().lower()
        return email, not email
    if auth.get("loggedIn"):
        return (auth.get("email") or "").strip().lower(), False
    return "", False


def _register(email: str, org, method, timeout: float = 5) -> dict:
    r = _request("POST", "/rest/v1/rpc/rk_register", {
        "p_email": email, "p_org": org, "p_auth_method": method,
        "p_machine": MACHINE, "p_version": _ctx["version"],
    }, timeout=timeout) or {}
    _registered.add(email)
    state = {"email": email, "active": bool(r.get("active", True)), "admin": bool(r.get("admin")),
             "checked": time.time()}
    _save_state(state)
    return state


def sync(auth: dict, version: str) -> dict:
    """Called with every /api/status. Registers the user (at most every few minutes) and says whether
    they may use ReviewKit. Offline, the last answer for the same e-mail is used; with none, they may."""
    if not ENABLED:
        return {"enabled": False}
    _ctx["version"] = version
    email, needs_email = _identity(auth)
    org, method = auth.get("orgName"), auth.get("authMethod")
    changed = email != _ctx["email"]
    _ctx.update(email=email, org=org, method=method, synced=True)
    _start_flusher()
    if not email:
        return {"enabled": True, "email": "", "needs_email": needs_email, "active": True, "admin": False}

    state = _load_state()
    same = state.get("email") == email
    offline = False
    if changed or not same or time.time() - state.get("checked", 0) > REGISTER_EVERY:
        try:
            state = _register(email, org, method)
        except Exception as e:  # noqa: BLE001  offline, Supabase down, …
            offline = True
            if not same:
                state = {"email": email, "active": True, "admin": False}
            if isinstance(e, SupabaseError) and e.status < 500:
                print(f"[activity] register failed: {e}", flush=True)
    if not _ctx["opened"]:
        _ctx["opened"] = True
        log("app.open")
    elif changed:
        log("claude.sign_in")
    _wake.set()
    return {"enabled": True, "email": email, "needs_email": False, "offline": offline,
            "active": bool(state.get("active", True)), "admin": bool(state.get("admin"))}


def ensure_synced() -> None:
    if ENABLED and not _ctx["synced"]:
        from . import claude_runner  # late import: claude_runner is heavy and imports settings itself

        sync(claude_runner.auth_status(), _ctx["version"])


def access_error() -> str:
    """Why this user can't run reviews right now, or "" if they can."""
    if not ENABLED:
        return ""
    if not _ctx["email"]:
        if (settings.load().get("api_key") or "").strip():
            return "Enter your work e-mail in Settings → Activity before running reviews."
        return ""  # not signed in at all: the review itself reports that
    state = _load_state()
    if state.get("email") == _ctx["email"] and state.get("active") is False:
        return "Your access to ReviewKit has been turned off. Ask the ReviewKit admin to turn it back on."
    return ""


# ---------- activity log ----------

def log(action: str, **details) -> None:
    """Queue one event for the current user. Never raises and never blocks on the network."""
    if not ENABLED or not _ctx["email"]:
        return
    ev = {"email": _ctx["email"], "action": action, "occurred_at": _now(),
          "details": {k: v for k, v in details.items() if v is not None and v != ""}}
    try:
        with _lock:
            with open(_QUEUE, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
    except OSError:
        return
    _start_flusher()
    _wake.set()


def _read_queue() -> list[dict]:
    try:
        lines = _QUEUE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-MAX_QUEUE:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _drop_sent(count: int) -> None:
    """Remove the first `count` events (new ones may have been appended meanwhile)."""
    with _lock:
        rest = _read_queue()[count:]
        try:
            _QUEUE.write_text("".join(json.dumps(e) + "\n" for e in rest), encoding="utf-8")
        except OSError:
            pass


def _flush() -> bool:
    """Send queued events. Returns False if Supabase couldn't be reached (try again later)."""
    events = _read_queue()
    if not events:
        return True
    email = events[0].get("email") or ""
    batch = []
    for e in events[:BATCH]:  # one user per call, keeping the queue order
        if e.get("email") != email:
            break
        batch.append({k: e[k] for k in ("action", "details", "occurred_at") if k in e})
    try:
        if email and email not in _registered:
            if email == _ctx["email"]:
                _register(email, _ctx["org"], _ctx["method"], timeout=10)
            else:
                _register(email, None, None, timeout=10)
        if email:
            _request("POST", "/rest/v1/rpc/rk_log", {
                "p_email": email, "p_machine": MACHINE, "p_version": _ctx["version"], "p_events": batch,
            }, timeout=15)
    except SupabaseError as e:
        if e.status >= 500:
            return False
        print(f"[activity] dropped {len(batch)} event(s): {e}", flush=True)  # bad data: retrying won't help
    except Exception:  # noqa: BLE001  offline
        return False
    _drop_sent(len(batch))
    return True


def _run_flusher() -> None:
    delay = 5
    while True:
        _wake.wait(timeout=60)
        _wake.clear()
        time.sleep(2)  # collect events that arrive together
        while _read_queue():
            if not _flush():
                time.sleep(delay)
                delay = min(delay * 2, 600)
                break
            delay = 5


def _start_flusher() -> None:
    if ENABLED and _flusher["thread"] is None:
        t = threading.Thread(target=_run_flusher, name="activity-flush", daemon=True)
        _flusher["thread"] = t
        t.start()


# ---------- admin page ----------

_admin: dict = {"access": "", "refresh": "", "expires": 0.0, "email": ""}


class AdminAuthError(Exception):
    pass


def admin_status() -> dict:
    return {"signed_in": bool(_admin["refresh"]), "email": _admin["email"]}


def _keep_session(r: dict) -> None:
    _admin.update(access=r["access_token"], refresh=r["refresh_token"],
                  expires=time.time() + float(r.get("expires_in", 3600)) - 60,
                  email=((r.get("user") or {}).get("email") or _admin["email"]))


def admin_sign_in(email: str, password: str) -> dict:
    """Password sign-in for the admin user created in the Supabase dashboard."""
    try:
        r = _request("POST", "/auth/v1/token?grant_type=password",
                     {"email": email.strip().lower(), "password": password}, timeout=15)
    except SupabaseError as e:
        if e.status in (400, 401, 422):
            raise AdminAuthError("Wrong e-mail or password.") from None
        raise
    _keep_session(r)
    if not _request("POST", "/rest/v1/rpc/rk_is_admin", {}, token=_admin["access"]):
        admin_sign_out()
        raise AdminAuthError("That e-mail isn't a ReviewKit admin.")
    return admin_status()


def _token() -> str:
    if not _admin["refresh"]:
        raise AdminAuthError("Sign in to the Users page first.")
    if time.time() >= _admin["expires"]:
        try:
            r = _request("POST", "/auth/v1/token?grant_type=refresh_token",
                         {"refresh_token": _admin["refresh"]}, timeout=15)
        except SupabaseError:
            admin_sign_out()
            raise AdminAuthError("Your admin session expired. Sign in again.") from None
        _keep_session(r)
    return _admin["access"]


def admin_sign_out() -> None:
    token = _admin["access"]
    _admin.update(access="", refresh="", expires=0.0, email="")
    if token:
        try:
            _request("POST", "/auth/v1/logout", {}, token=token, timeout=5)
        except Exception:  # noqa: BLE001
            pass


def admin_users() -> list[dict]:
    return _request("GET", "/rest/v1/rk_users?select=*&order=last_seen.desc", token=_token()) or []


def admin_logs(email: str = "", action: str = "", before_id: int | None = None, limit: int = 200) -> list[dict]:
    q = [("select", "*"), ("order", "id.desc"), ("limit", str(max(1, min(limit, 500))))]
    if email:
        q.append(("email", f"eq.{email.strip().lower()}"))
    if action:
        q.append(("action", f"like.{action.strip()}*"))
    if before_id:
        q.append(("id", f"lt.{int(before_id)}"))
    return _request("GET", "/rest/v1/rk_audit_logs?" + urllib.parse.urlencode(q), token=_token()) or []


def admin_set_active(email: str, active: bool) -> dict:
    rows = _request("PATCH", "/rest/v1/rk_users?" + urllib.parse.urlencode([("email", f"eq.{email.strip().lower()}")]),
                    {"is_active": bool(active)}, token=_token(), prefer="return=representation") or []
    if not rows:
        raise AdminAuthError("That user wasn't found, or your account can't change users.")
    return rows[0]
