"""Picks the review rules automatically, so nobody can run a review with the wrong rules.

The repository's project files say which stacks it contains:
  .NET         -> *.sln / *.csproj
  Python       -> pyproject.toml / requirements*.txt / setup.py / Pipfile / manage.py ...
                  (or, when nothing else is found, any *.py file)
  Angular      -> angular.json (its folder is the Angular project)
  Ionic        -> ionic.config.json / capacitor.config.* (its folder is the Ionic project;
                  Ionic apps also have angular.json or React, so Ionic wins for that folder)
  React        -> package.json that depends on react (its folder is the React project)
  React Native -> package.json that depends on react-native or expo (wins over React)

A repository can hold more than one (e.g. a .NET API with an Angular ClientApp, or a Django API with a
React front end). Then the files a change touches decide: C# and Python source files always belong to
.NET / Python; every other file belongs to the deepest Angular/Ionic/React/React Native project folder
that contains it, otherwise to .NET or Python if it is one of their project files.
"""
import json
from pathlib import PurePosixPath

from . import git_ops
from .skills import REVIEWS

# Source files that can only belong to one stack, wherever they sit in the repository.
DOTNET_SOURCE = {".cs", ".csproj", ".sln", ".cshtml", ".razor"}
PYTHON_SOURCE = {".py", ".pyi", ".ipynb"}
# Also theirs when no front-end project folder claims the file.
DOTNET_EXT = DOTNET_SOURCE | {".props", ".targets", ".resx", ".config", ".sql"}
PYTHON_FILES = {"pyproject.toml", "setup.py", "setup.cfg", "pipfile", "pipfile.lock", "poetry.lock", "uv.lock",
                "tox.ini", "pytest.ini", "environment.yml", "manage.py"}

ROOTED = ["ionic", "angular", "reactnative", "react"]  # project-folder stacks; earlier wins for the same folder
ORDER = ["capataz", "python", "angular", "ionic", "react", "reactnative"]  # back end first when a change spans stacks


def _folder(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent + "/"


def _is_python_project_file(name: str) -> bool:
    return name in PYTHON_FILES or (name.startswith("requirements") and name.endswith(".txt"))


def _package_kind(repo: str, path: str) -> str | None:
    """'reactnative' / 'react' when this package.json depends on them."""
    try:
        pkg = json.loads(git_ops.read_file(repo, path) or "{}")
    except ValueError:
        return None
    if not isinstance(pkg, dict):
        return None
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    if "react-native" in deps or "expo" in deps:
        return "reactnative"
    return "react" if "react" in deps else None


def scan(repo: str) -> dict:
    """Stacks found in the repository: {"types": [...], "roots": {"angular": [...], "ionic": [...], ...}}."""
    roots: dict[str, set[str]] = {k: set() for k in ROOTED}
    dotnet = python = py_source = False
    for f in git_ops.ls_files(repo):
        if "node_modules/" in f:
            continue
        name = f.rsplit("/", 1)[-1].lower()
        if name == "ionic.config.json" or name.startswith("capacitor.config."):
            roots["ionic"].add(_folder(f))
        elif name == "angular.json":
            roots["angular"].add(_folder(f))
        elif name == "package.json":
            kind = _package_kind(repo, f)
            if kind:
                roots[kind].add(_folder(f))
        elif name.endswith((".sln", ".csproj")):
            dotnet = True
        elif _is_python_project_file(name):
            python = True
        elif name.endswith(".py"):
            py_source = True
    claimed: set[str] = set()
    for kind in ROOTED:  # one stack per folder
        roots[kind] -= claimed
        claimed |= roots[kind]
    present = {"capataz": dotnet, "python": python, **{k: bool(v) for k, v in roots.items()}}
    if py_source and not any(present.values()):  # plain scripts without a project file
        present["python"] = True
    return {"types": [t for t in ORDER if present[t]], "roots": {k: sorted(v) for k, v in roots.items()}}


def classify(path: str, stacks: dict) -> str | None:
    types = stacks["types"]
    p = PurePosixPath(path)
    ext, name = p.suffix.lower(), p.name.lower()
    if "capataz" in types and ext in DOTNET_SOURCE:
        return "capataz"
    if "python" in types and ext in PYTHON_SOURCE:
        return "python"
    best, best_len = None, -1
    for kind in ROOTED:
        for root in stacks["roots"].get(kind, []):
            if path.startswith(root) and len(root) > best_len:
                best, best_len = kind, len(root)
    if best:
        return best
    if "capataz" in types and ext in DOTNET_EXT:
        return "capataz"
    if "python" in types and _is_python_project_file(name):
        return "python"
    return None


def primary(stacks: dict) -> str | None:
    types = stacks["types"]
    return types[0] if types else None  # ORDER puts the back end first for an API with a client app


def for_change(stacks: dict, paths: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Review types for a change, plus the changed files that belong to each."""
    groups: dict[str, list[str]] = {}
    for p in paths:
        kind = classify(p, stacks)
        if kind:
            groups.setdefault(kind, []).append(p)
    types = [t for t in ORDER if t in groups]
    if not types:  # only docs/config changed: use the repository's main stack
        main = primary(stacks)
        types = [main] if main else []
    return types, groups


def label(types: list[str]) -> str:
    return " + ".join(REVIEWS[t]["label"] for t in types)


def describe(stacks: dict) -> dict:
    """What the UI shows after a repository is opened."""
    types = stacks["types"]
    if not types:
        return {"types": [], "label": "", "detail": ""}
    if len(types) == 1:
        detail = "Detected from the repository's project files."
    else:
        where = [f"{REVIEWS[k]['label']} in {r or 'the repository root'}"
                 for k in ORDER for r in stacks["roots"].get(k, [])]
        detail = ("This repository has more than one kind of project" + (" (" + "; ".join(where) + ")" if where else "")
                  + ". Each changed file is reviewed with the rules for its project.")
    return {"types": types, "label": label(types), "detail": detail}
