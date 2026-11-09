"""Picks the review rules automatically, so nobody can run a review with the wrong rules.

The repository's project files say which stacks it contains:
  .NET    -> *.sln / *.csproj
  Angular -> angular.json (its folder is the Angular project)
  Ionic   -> ionic.config.json / capacitor.config.* (its folder is the Ionic project;
             Ionic apps also have angular.json, so Ionic wins for that folder)

A repository can hold more than one (e.g. a .NET API with an Angular ClientApp). Then the files a
change touches decide: each changed file belongs to the deepest Angular/Ionic project folder that
contains it, otherwise to .NET if it is a .NET source file.
"""
from pathlib import PurePosixPath

from . import git_ops
from .skills import REVIEWS

DOTNET_EXT = {".cs", ".csproj", ".sln", ".cshtml", ".razor", ".props", ".targets", ".resx", ".config", ".sql"}
ORDER = ["capataz", "angular", "ionic"]  # .NET first when a change spans stacks


def _folder(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent + "/"


def scan(repo: str) -> dict:
    """Stacks found in the repository: {"types": [...], "roots": {"angular": [...], "ionic": [...]}}."""
    ionic, angular, dotnet = set(), set(), False
    for f in git_ops.ls_files(repo):
        if "node_modules/" in f:
            continue
        name = f.rsplit("/", 1)[-1].lower()
        if name == "ionic.config.json" or name.startswith("capacitor.config."):
            ionic.add(_folder(f))
        elif name == "angular.json":
            angular.add(_folder(f))
        elif name.endswith((".sln", ".csproj")):
            dotnet = True
    angular -= ionic
    types = [t for t, present in (("capataz", dotnet), ("angular", bool(angular)), ("ionic", bool(ionic))) if present]
    return {"types": types, "roots": {"angular": sorted(angular), "ionic": sorted(ionic)}}


def classify(path: str, stacks: dict) -> str | None:
    best, best_len = None, -1
    for kind in ("ionic", "angular"):
        for root in stacks["roots"][kind]:
            if path.startswith(root) and len(root) > best_len:
                best, best_len = kind, len(root)
    if best:
        return best
    if "capataz" in stacks["types"] and PurePosixPath(path).suffix.lower() in DOTNET_EXT:
        return "capataz"
    return None


def primary(stacks: dict) -> str | None:
    types = stacks["types"]
    return types[0] if types else None  # ORDER puts .NET first for an API with a ClientApp


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
                 for k in ("angular", "ionic") for r in stacks["roots"][k]]
        detail = ("This repository has more than one kind of project (" + "; ".join(where) + "). "
                  "Each changed file is reviewed with the rules for its project.")
    return {"types": types, "label": label(types), "detail": detail}
