"""Git helpers: run against the developer's local clone, using their own git credentials."""
import subprocess
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_DIFF_CHARS = 350_000  # beyond this, Claude reads per-file diffs itself


class GitError(Exception):
    pass


def _git(repo: str, *args: str, timeout: int = 120) -> str:
    try:
        p = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW,
        )
    except FileNotFoundError:
        raise GitError("Git is not installed or not on PATH. Install Git for Windows and restart ReviewKit.")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout}s.")
    if p.returncode != 0:
        raise GitError((p.stderr or p.stdout).strip() or f"git {args[0]} failed")
    return p.stdout


def repo_root(path: str) -> str:
    if not path or not Path(path).is_dir():
        raise GitError("That folder does not exist.")
    try:
        return str(Path(_git(path, "rev-parse", "--show-toplevel").strip()))
    except GitError as e:
        if "not a git repository" in str(e).lower():
            raise GitError("That folder is not a Git repository. Pick the folder of your local clone.")
        raise


def repo_info(repo: str) -> dict:
    url = ""
    try:
        url = _git(repo, "remote", "get-url", "origin").strip()
    except GitError:
        pass
    return {"root": repo, "name": Path(repo).name, "origin": url}


def ls_files(repo: str) -> list[str]:
    """Tracked files (forward-slash paths relative to the repo root)."""
    return [f for f in _git(repo, "ls-files", "-z").split("\0") if f]


def changed_paths(name_status: str) -> list[str]:
    """Paths from `git diff --name-status` output (the new path for renames/copies)."""
    paths = []
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            paths.append(parts[-1].strip())
    return paths


def fetch(repo: str) -> None:
    _git(repo, "fetch", "--prune", "origin", timeout=300)


def branches(repo: str) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    names = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or line in ("origin", "origin/HEAD"):
            continue
        names.add(line.removeprefix("origin/"))
    return sorted(names, key=str.lower)


def _ref(repo: str, branch: str) -> str:
    branch = branch.strip().removeprefix("origin/")
    if not branch:
        raise GitError("Enter both a source and a target branch.")
    for ref in (f"origin/{branch}", branch):
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            return ref
        except GitError:
            continue
    raise GitError(f"Branch '{branch}' was not found locally or on origin. Click Fetch, then check the name.")


def diff(repo: str, source: str, target: str) -> dict:
    src, tgt = _ref(repo, source), _ref(repo, target)
    rng = f"{tgt}...{src}"
    files = _git(repo, "diff", "--name-status", rng)
    if not files.strip():
        raise GitError(f"No differences between {tgt} and {src}.")
    stat = _git(repo, "diff", "--stat", rng)
    commits = _git(repo, "log", "--oneline", "--no-merges", f"{tgt}..{src}")
    full = _git(repo, "diff", "--unified=5", rng)
    truncated = len(full) > MAX_DIFF_CHARS
    return {
        "range": rng, "source_ref": src, "target_ref": tgt,
        "stat": stat, "files": files, "commits": commits,
        "diff": "" if truncated else full,
        "truncated": truncated,
    }
