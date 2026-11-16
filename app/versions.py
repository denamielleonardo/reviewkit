"""Framework and library versions of a repository, read once and remembered.

ReviewKit reads the project files of the reviewed commit (*.csproj, package.json, pyproject.toml,
requirements*.txt ...) and puts the versions in the review prompt, so Claude doesn't spend tool calls
finding them and every review of the same code base starts from the same facts.

The result is saved in %APPDATA%\\ReviewKit\\versions.json, keyed by the repository and a fingerprint of
those project files (their git blob ids). It is reused until one of the files changes, is added or is
removed, e.g. after a framework upgrade or on a branch that bumps a package, and re-read only then.
"""
import hashlib
import json
import re
from pathlib import PurePosixPath

from . import git_ops
from .paths import data_dir

_FILE = data_dir() / "versions.json"
_FORMAT = 2          # bump when the saved shape or what is extracted changes
_MAX_REPOS = 30      # repositories remembered
_MAX_PER_REPO = 6    # fingerprints remembered per repository (branches on different versions)
_MAX_FILES = 150     # project files read per kind
_MAX_LINES = 40      # lines shown per kind in the prompt

DOTNET_FILES = {"directory.build.props", "directory.packages.props", "global.json", "packages.config"}
PYTHON_FILES = {"pyproject.toml", "setup.cfg", "setup.py", "pipfile", ".python-version", "runtime.txt", "environment.yml"}

NUGET_KEY = ("Microsoft.EntityFrameworkCore", "EntityFramework", "MediatR", "AutoMapper", "FluentValidation",
             "Microsoft.AspNetCore", "Microsoft.AspNet.", "Dapper", "Newtonsoft.Json", "Swashbuckle", "Serilog",
             "Hangfire", "Polly", "xunit", "NUnit", "MSTest", "Moq")
NPM_KEY = ("@angular/core", "@angular/cli", "typescript", "rxjs", "zone.js", "@ngrx/store", "@angular/material",
           "@ionic/angular", "@ionic/react", "@ionic/vue", "@ionic/core", "ionic-angular",
           "@capacitor/core", "cordova", "cordova-android", "cordova-ios",
           "react", "react-dom", "react-native", "expo", "expo-router", "next", "vite", "react-scripts",
           "react-router", "react-router-dom", "@remix-run/react",
           "@react-navigation/native", "react-native-reanimated", "react-native-gesture-handler",
           "@reduxjs/toolkit", "redux", "react-redux", "zustand", "mobx", "jotai",
           "@tanstack/react-query", "react-query", "swr", "@apollo/client",
           "react-hook-form", "formik", "vue", "jest", "vitest", "@testing-library/react")
PY_KEY = ("django", "djangorestframework", "fastapi", "flask", "starlette", "sqlalchemy", "alembic", "pydantic",
          "celery", "pandas", "numpy", "pyspark", "requests", "httpx", "aiohttp", "pytest", "mypy", "ruff")
_PY_DEP = re.compile(
    r"""^\s*["']?(""" + "|".join(PY_KEY) + r""")(\[[^\]]*\])?\s*((?:[=<>~!^]|\{|@)[^#\n]*?)["',]*\s*(?:#.*)?$""",
    re.I | re.M)


def _kind(path: str) -> str | None:
    if "node_modules/" in path:
        return None
    name = path.rsplit("/", 1)[-1].lower()
    if name == "package.json":
        return "js"
    if name.endswith((".csproj", ".vbproj", ".fsproj")) or name in DOTNET_FILES:
        return "dotnet"
    if name in PYTHON_FILES or (name.startswith("requirements") and name.endswith(".txt")):
        return "python"
    return None


# ---------- .NET ----------

def _tag(xml: str, name: str) -> str:
    m = re.search(rf"<{name}\b[^>]*>\s*([^<]+?)\s*</{name}>", xml, re.I)
    return m.group(1) if m else ""


def _default_csharp(tfm: str) -> str:
    t = tfm.lower().split(";")[0].strip()
    m = re.match(r"net(\d+)\.\d", t)
    if m and int(m.group(1)) >= 5:
        return str(int(m.group(1)) + 4)  # .NET 5 -> C# 9, one language version per release since
    if t.startswith("netcoreapp3") or t == "netstandard2.1":
        return "8"
    if t.startswith(("netcoreapp", "netstandard", "net4", "v4", "net3", "v3", "net2", "v2")):
        return "7.3"
    return ""


def _packages(xml: str, element: str, central: dict[str, str]) -> list[tuple[str, str]]:
    found = []
    for m in re.finditer(rf"<{element}\b([^>]*?)(/?)>", xml, re.I):
        attrs = dict((k.lower(), v) for k, v in re.findall(r"""([\w.]+)\s*=\s*["']([^"']*)["']""", m.group(1)))
        name = attrs.get("include") or attrs.get("update") or attrs.get("id")
        if not name or not name.startswith(NUGET_KEY):
            continue
        version = attrs.get("version") or central.get(name.lower(), "")
        if not version and not m.group(2):  # <PackageReference Include="X"><Version>1.2</Version></PackageReference>
            version = _tag(re.split(rf"</{element}", xml[m.end(): m.end() + 300], 1, flags=re.I)[0], "Version")
        found.append((name, version))
    return found


def _pk_text(pk: list[tuple[str, str]]) -> str:
    return ", ".join(f"{n} {v}".strip() for n, v in pk[:15])


def _dotnet(files: list[tuple[str, str]], out: dict) -> list[str]:
    """Prompt lines; fills out[path] = {"tfm", "csharp", "packages"} for the UI summary."""
    texts = dict(files)
    central: dict[str, str] = {}
    for path, text in files:
        if path.lower().endswith("directory.packages.props"):
            for m in re.finditer(r"<PackageVersion\b([^>]*)>", text, re.I):
                a = dict((k.lower(), v) for k, v in re.findall(r"""([\w.]+)\s*=\s*["']([^"']*)["']""", m.group(1)))
                if a.get("include") and a.get("version"):
                    central[a["include"].lower()] = a["version"]
    lines = []
    for path in sorted(texts):
        text, name = texts[path], path.rsplit("/", 1)[-1].lower()
        if name == "global.json":
            try:
                sdk = (json.loads(text).get("sdk") or {}).get("version")
            except (ValueError, AttributeError):
                sdk = None
            if sdk:
                lines.append(f"`{path}`: .NET SDK {sdk}")
            continue
        if name == "directory.packages.props":
            continue  # folded into each project's package versions
        if name == "packages.config":
            pk = _packages(text, "package", {})
            if pk:
                lines.append(f"`{path}`: " + _pk_text(pk))
                out[path] = {"packages": dict(pk)}
            continue
        tfm = _tag(text, "TargetFrameworks") or _tag(text, "TargetFramework")
        old = _tag(text, "TargetFrameworkVersion")
        facts = []
        if tfm:
            facts.append(f"target framework {tfm}")
        elif old:
            facts.append(f".NET Framework {old.lstrip('vV')}")
        lang = _tag(text, "LangVersion")
        default = _default_csharp(tfm or old)
        if lang:
            facts.append(f"C# {lang} (LangVersion)")
        elif default:
            facts.append(f"C# {default} (default for this target)")
        nullable = _tag(text, "Nullable")
        if nullable:
            facts.append(f"nullable {nullable}")
        if _tag(text, "ImplicitUsings").lower() in ("enable", "true"):
            facts.append("implicit usings on")
        pk = _packages(text, "PackageReference", central)
        if pk:
            facts.append("packages: " + _pk_text(pk))
        if facts:
            lines.append(f"`{path}`: " + "; ".join(facts))
            out[path] = {"tfm": tfm or old, "csharp": lang or default, "packages": dict(pk)}
    return lines


# ---------- JavaScript / TypeScript ----------

def _js(files: list[tuple[str, str]], out: dict) -> list[str]:
    """Prompt lines; fills out[path] = {package: version} for the UI summary."""
    lines = []
    for path, text in sorted(files):
        try:
            pkg = json.loads(text)
        except ValueError:
            continue
        if not isinstance(pkg, dict):
            continue
        deps: dict[str, str] = {}
        for key in ("peerDependencies", "devDependencies", "dependencies"):  # dependencies win
            if isinstance(pkg.get(key), dict):
                deps.update({k: str(v) for k, v in pkg[key].items()})
        found = [f"{k} {deps[k]}" for k in NPM_KEY if k in deps]
        if not found:
            continue
        out[path] = {k: deps[k] for k in NPM_KEY if k in deps}
        node = (pkg.get("engines") or {}).get("node") if isinstance(pkg.get("engines"), dict) else None
        if node:
            found.append(f"node {node}")
        lines.append(f"`{path}`: " + ", ".join(found))
    return lines


# ---------- Python ----------

def _python(files: list[tuple[str, str]], out: dict) -> list[str]:
    """Prompt lines; fills out[path] = {"python" or library (lower case): version} for the UI summary."""
    lines = []
    for path, text in sorted(files):
        name = path.rsplit("/", 1)[-1].lower()
        found: dict[str, str] = {}
        if name in (".python-version", "runtime.txt"):
            first = text.strip().splitlines()[0].strip() if text.strip() else ""
            if first:
                found["python"] = first.removeprefix("python-")
        else:
            m = re.search(r"""(?im)^\s*(?:requires-python|python_requires|python_version|python)\s*=\s*["']?([^"'\n,#]+)""", text)
            if m:
                found["python"] = m.group(1).strip()
            m = re.search(r"(?im)^\s*-\s*python\s*=+\s*([\w.*]+)", text) if name == "environment.yml" else None
            if m:
                found["python"] = m.group(1)
            names = {}
            for d in _PY_DEP.finditer(text):
                lib = d.group(1).lower()
                if lib not in found:
                    spec = d.group(3).strip()
                    if spec.startswith("=") and not spec.startswith("=="):  # Poetry/Pipfile: django = "^4.2"
                        spec = spec[1:]
                    found[lib] = spec.strip().strip(chr(34) + chr(39) + ",")
                    names[lib] = d.group(1)
        if found:
            facts = [f"{'Python' if k == 'python' else names.get(k, k)} {v}".strip() for k, v in found.items()]
            lines.append(f"`{path}`: " + ", ".join(facts[:20]))
            out[path] = found
    return lines


# ---------- remembered results ----------

def _load() -> dict:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("format") == _FORMAT else {}
    except Exception:
        return {}


def _remember(repo_key: str, fingerprint: str, found: dict) -> None:
    data = _load() or {"format": _FORMAT, "repos": {}}
    repos: dict = data.setdefault("repos", {})
    entry = repos.pop(repo_key, {})
    entry.pop(fingerprint, None)
    entry[fingerprint] = found  # newest last
    repos[repo_key] = dict(list(entry.items())[-_MAX_PER_REPO:])
    data["repos"] = dict(list(repos.items())[-_MAX_REPOS:])
    try:
        _FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # remembering is only a speed-up


def for_commit(repo: str, ref: str) -> dict:
    """{"dotnet": [...], "js": [...], "python": [...], "cached": bool}: one line per project file."""
    by_kind: dict[str, list[tuple[str, str]]] = {"dotnet": [], "js": [], "python": []}
    for blob, path in git_ops.ls_tree(repo, ref):
        kind = _kind(path)
        if kind:
            by_kind[kind].append((blob, path))
    for kind in by_kind:  # shallowest first, so a huge monorepo keeps its main projects
        by_kind[kind] = sorted(by_kind[kind], key=lambda bp: (bp[1].count("/"), bp[1]))[:_MAX_FILES]
    fingerprint = hashlib.sha1(
        json.dumps(sorted(b + ":" + p for files in by_kind.values() for b, p in files)).encode()).hexdigest()
    repo_key = str(repo).replace("\\", "/").lower()
    saved = _load().get("repos", {}).get(repo_key, {}).get(fingerprint)
    if saved:
        return {**saved, "cached": True}
    read = {k: [(p, git_ops.cat_blob(repo, b)) for b, p in files] for k, files in by_kind.items()}
    facts: dict[str, dict] = {"dotnet": {}, "js": {}, "python": {}}
    found = {
        "dotnet": _dotnet(read["dotnet"], facts["dotnet"]),
        "js": _js(read["js"], facts["js"]),
        "python": _python(read["python"], facts["python"]),
        "facts": facts,
    }
    _remember(repo_key, fingerprint, found)
    return {**found, "cached": False}


# ---------- short summary for the UI ----------

def _clean(v: str) -> str:
    """'^17.3.0' -> '17.3.0', '==4.2.11' -> '4.2.11'; ranges such as '>=3.10' are kept as written."""
    v = str(v).strip().strip("\"'")
    if "{" in v or v in ("*", ""):
        return ""
    return v.removeprefix("==").removeprefix("~=").lstrip("^~").strip()


def _net_name(tfm: str) -> str:
    t = tfm.strip().lower()
    if m := re.fullmatch(r"netcoreapp(\d+\.\d+)", t):
        return f".NET Core {m.group(1)}"
    if m := re.fullmatch(r"netstandard(\d+\.\d+)", t):
        return f".NET Standard {m.group(1)}"
    if m := re.fullmatch(r"net(\d+)\.(\d+)(-[\w.]+)?", t):
        return f".NET {m.group(1)}" + (f".{m.group(2)}" if m.group(2) != "0" else "")
    if m := re.fullmatch(r"net(\d)(\d)(\d?)", t):  # net48, net472
        return ".NET Framework " + ".".join(d for d in m.groups() if d)
    if m := re.fullmatch(r"v(\d+(?:\.\d+)*)", t):
        return f".NET Framework {m.group(1)}"
    return tfm


def _distinct(values) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def _dotnet_summary(facts: dict) -> str:
    projects = [f for p, f in facts.items() if p.lower().endswith(("proj", ".csproj"))] or list(facts.values())
    frameworks = _distinct(_net_name(t) for f in projects for t in str(f.get("tfm") or "").split(";") if t.strip())
    csharp = _distinct(f.get("csharp") for f in projects)
    parts = []
    if frameworks:
        parts.append(", ".join(frameworks[:3]) + (" …" if len(frameworks) > 3 else ""))
    if csharp:
        parts.append("C# " + ", ".join(csharp[:3]))
    ef_core = _distinct(_clean(v) for f in facts.values() for n, v in (f.get("packages") or {}).items()
                        if n.lower().startswith("microsoft.entityframeworkcore"))
    ef6 = _distinct(_clean(v) for f in facts.values() for n, v in (f.get("packages") or {}).items()
                    if n.lower() == "entityframework")
    if ef_core:
        parts.append("EF Core " + ", ".join(ef_core[:2]))
    if ef6:
        parts.append("Entity Framework " + ", ".join(ef6[:2]))
    return " · ".join(parts)


def _js_summary(kind: str, deps: dict) -> str:
    def v(name):
        return _clean(deps.get(name, ""))

    parts = []
    if kind == "angular":
        parts += [f"Angular {v('@angular/core')}" if v("@angular/core") else ""]
    elif kind == "ionic":
        ion = next((n for n in ("@ionic/angular", "@ionic/react", "@ionic/vue", "@ionic/core", "ionic-angular") if v(n)), "")
        parts.append(f"Ionic {v(ion)}" if ion else "")
        parts.append(f"Capacitor {v('@capacitor/core')}" if v("@capacitor/core") else
                     f"Cordova {v('cordova-android') or v('cordova-ios') or v('cordova')}"
                     if (v("cordova-android") or v("cordova-ios") or v("cordova")) else "")
        for name, label in (("@angular/core", "Angular"), ("react", "React"), ("vue", "Vue")):
            if v(name):
                parts.append(f"{label} {v(name)}")
                break
    elif kind == "react":
        parts.append(f"React {v('react')}" if v("react") else "")
        for name, label in (("next", "Next.js"), ("@remix-run/react", "Remix"), ("vite", "Vite"), ("react-scripts", "Create React App")):
            if v(name):
                parts.append(f"{label} {v(name)}")
                break
    elif kind == "reactnative":
        parts.append(f"React Native {v('react-native')}" if v("react-native") else "")
        parts.append(f"Expo SDK {v('expo').split('.')[0]}" if v("expo") else "")
    if kind == "angular" and v("rxjs"):
        parts.append(f"RxJS {v('rxjs')}")
    if v("typescript"):
        parts.append(f"TypeScript {v('typescript')}")
    return " · ".join(p for p in parts if p)


_PY_SHOW = (("django", "Django"), ("fastapi", "FastAPI"), ("flask", "Flask"), ("sqlalchemy", "SQLAlchemy"),
            ("pydantic", "Pydantic"), ("pandas", "pandas"), ("celery", "Celery"))


def _python_summary(facts: dict) -> str:
    merged: dict[str, str] = {}
    for f in facts.values():
        for k, val in f.items():
            if k not in merged and _clean(val):
                merged[k] = _clean(val)
    parts = [f"Python {merged['python']}"] if "python" in merged else []
    parts += [f"{label} {merged[k]}" for k, label in _PY_SHOW if k in merged][:3]
    return " · ".join(parts)


def summary(found: dict, stacks: dict) -> list[dict]:
    """[{"type", "text"}] — a one-line version summary per detected review type (types with nothing found are left out)."""
    facts = found.get("facts") or {}
    js = facts.get("js") or {}
    rows = []
    for kind in stacks.get("types", []):
        if kind == "capataz":
            texts = [_dotnet_summary(facts.get("dotnet") or {})]
        elif kind == "python":
            texts = [_python_summary(facts.get("python") or {})]
        else:
            roots = stacks.get("roots", {}).get(kind, [])
            texts = [_js_summary(kind, js.get(r + "package.json") or {}) for r in roots]
            if not any(texts):  # e.g. angular.json without a package.json beside it: nearest one above
                for r in roots:
                    above = [p for p in js if r.startswith(p[: -len("package.json")])]
                    if above:
                        texts.append(_js_summary(kind, js[max(above, key=len)]))
        texts = _distinct(t for t in texts if t)
        if texts:
            rows.append({"type": kind, "text": "  |  ".join(texts[:2]) + (" …" if len(texts) > 2 else "")})
    return rows


_SECTIONS = [
    ("dotnet", ".NET", {"capataz"}),
    ("js", "JavaScript / TypeScript", {"angular", "ionic", "react", "reactnative"}),
    ("python", "Python", {"python"}),
]


def prompt_section(found: dict, types: list[str], changed: list[str]) -> str:
    """Markdown for the review prompt: the versions that matter for these review types."""
    parts = []
    for key, title, kinds in _SECTIONS:
        lines = found.get(key) or []
        if not lines or not kinds & set(types):
            continue
        if len(lines) > _MAX_LINES:  # keep the projects closest to the changed files
            lines = sorted(lines, key=lambda l: -_closeness(l, changed))[:_MAX_LINES]
            lines.append("(more project files exist;read them yourself if a changed file belongs to one not listed)")
        parts.append(f"### {title}\n" + "\n".join(f"- {l}" for l in lines))
    if not parts:
        return ""
    return (
        "## Versions in this repository\n"
        "ReviewKit read these from the project files of the source branch. They are the result of the "
        "stack/version detection step in your review rules: use them as given, do not spend tool calls "
        "re-reading project files to find versions, and only look further if a version you need is not listed. "
        "Each changed file is governed by the nearest project file above it. Every suggestion and snippet must "
        "work on these versions.\n\n" + "\n\n".join(parts)
    )


def _closeness(line: str, changed: list[str]) -> int:
    """How many changed files sit under this project file's folder (deeper folders count more)."""
    m = re.match(r"`([^`]+)`", line)
    if not m:
        return 0
    folder = str(PurePosixPath(m.group(1)).parent)
    if folder == ".":
        return 1
    return sum(1 for c in changed if c.startswith(folder + "/")) * (folder.count("/") + 2)
