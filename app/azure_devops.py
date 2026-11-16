"""Pull requests on Azure DevOps, signed in with the credential Git already uses for the repository.

The token comes from `git credential fill` (Git Credential Manager), the same sign-in `git fetch` uses,
so developers don't create or paste a personal access token. If Git has no saved sign-in yet, Git
Credential Manager shows its normal Microsoft sign-in window.
"""
import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import git_ops, net

API = "api-version=7.1"
TITLE_MAX = 400          # Azure DevOps limit
DESCRIPTION_MAX = 4000   # Azure DevOps limit


class AzureError(Exception):
    pass


@dataclass
class Repo:
    org: str
    project: str
    repo: str
    base: str        # https://dev.azure.com/<org> or https://<org>.visualstudio.com
    host: str
    user: str        # user name from the remote URL, if any
    path: str        # remote URL path, used to pick the right saved credential

    def api(self, tail: str) -> str:
        q = urllib.parse.quote
        return f"{self.base}/{q(self.project)}/_apis/git/repositories/{q(self.repo)}/{tail}"

    def project_api(self, tail: str) -> str:
        return f"{self.base}/{urllib.parse.quote(self.project)}/_apis/git/{tail}"

    def web(self, tail: str = "") -> str:
        q = urllib.parse.quote
        return f"{self.base}/{q(self.project)}/_git/{q(self.repo)}" + (f"/{tail}" if tail else "")

    @property
    def name(self) -> str:
        return f"{self.project} / {self.repo}"


def parse_remote(url: str) -> Repo | None:
    """Azure DevOps repository from a remote URL (https dev.azure.com / visualstudio.com, or SSH)."""
    u, unq = url.strip(), urllib.parse.unquote
    m = re.match(r"https://(?:([^@/]+)@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/?#]+)", u, re.I)
    if m:
        user, org, project, repo = m.groups()
        host, base = "dev.azure.com", f"https://dev.azure.com/{urllib.parse.quote(unq(org))}"
    else:
        m = re.match(r"https://(?:([^@/]+)@)?([\w-]+)\.visualstudio\.com/(?:DefaultCollection/)?([^/]+)/_git/([^/?#]+)", u, re.I)
        if m:
            user, org, project, repo = m.groups()
            host, base = f"{org}.visualstudio.com", f"https://{org}.visualstudio.com"
        else:
            m = re.match(r"(?:ssh://)?[^@/]+@(?:ssh\.dev\.azure\.com|vs-ssh\.visualstudio\.com)(?::22)?[:/]v3/([^/]+)/([^/]+)/([^/?#]+)", u, re.I)
            if not m:
                return None
            org, project, repo = m.groups()
            user, host, base = None, "dev.azure.com", f"https://dev.azure.com/{urllib.parse.quote(unq(org))}"
    path = f"{unq(org)}/{unq(project)}/_git/{unq(repo)}" if host == "dev.azure.com" else f"{unq(project)}/_git/{unq(repo)}"
    return Repo(org=unq(org), project=unq(project), repo=unq(repo).removesuffix(".git"), base=base, host=host,
                user=unq(user or ""), path=urllib.parse.quote(path))


def from_repo(root: str) -> Repo:
    url = git_ops.repo_info(root).get("origin", "")
    repo = parse_remote(url) if url else None
    if not repo:
        raise AzureError("Pull requests can only be created for Azure DevOps repositories, and this repository's "
                         f"origin is {url or 'not set'}.")
    return repo


def _credential(repo: Repo, oauth: bool = False) -> tuple[str, str]:
    """Git's saved sign-in for this repository.

    oauth=True asks Git Credential Manager for a Microsoft sign-in token instead of its usual
    code-only token, just for this call (the user's Git settings are not changed). That token can
    look people up; the first time, Git Credential Manager shows the Microsoft sign-in window."""
    lines = ["protocol=https", f"host={repo.host}", f"path={repo.path}"]
    if repo.user:
        lines.append(f"username={repo.user}")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if oauth:
        env["GCM_AZREPOS_CREDENTIALTYPE"] = "oauth"
    try:
        p = subprocess.run(["git", "credential", "fill"], input=("\n".join(lines) + "\n\n").encode(),
                           capture_output=True, timeout=300, creationflags=git_ops.NO_WINDOW, env=env)
    except FileNotFoundError:
        raise AzureError("Git is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise AzureError("Signing in to Azure DevOps took too long. Try again, or run git fetch in this repository first.")
    fields = dict(l.split("=", 1) for l in p.stdout.decode("utf-8", "replace").splitlines() if "=" in l)
    if p.returncode != 0 or not fields.get("password"):
        if oauth:
            raise AzureError("ReviewKit needs you to sign in to Azure DevOps with your Microsoft account to look up "
                             "reviewers, and the sign-in didn't finish. Try again and complete the sign-in window.")
        raise AzureError("Git has no Azure DevOps sign-in for this repository. Run git fetch in it once (or click "
                         "Fetch in ReviewKit) to sign in, then try again.")
    return fields.get("username") or repo.user or "reviewkit", fields["password"]


def _request(repo: Repo, method: str, url: str, body: dict | None = None, oauth: bool = False) -> dict:
    user, token = _credential(repo, oauth)
    if oauth:
        authz = f"Bearer {token}"
    else:
        authz = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": authz, "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with net.urlopen(req, timeout=60) as r:
            ctype, raw = r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw).get("message", "")
        except ValueError:
            msg = ""
        if e.code in (401, 403):
            raise AzureError("Azure DevOps refused the request: your Git sign-in may have expired or lacks access "
                             "to create pull requests. Run git fetch in this repository to sign in again.")
        raise AzureError(f"Azure DevOps: {msg or f'HTTP {e.code}'}")
    except urllib.error.URLError as e:
        raise AzureError(f"Couldn't reach Azure DevOps: {e.reason}")
    if "json" not in ctype:  # a sign-in page instead of data means the token wasn't accepted
        raise AzureError("Azure DevOps didn't accept your Git sign-in. Run git fetch in this repository to sign in again.")
    return json.loads(raw)


def _pr(repo: Repo, data: dict) -> dict:
    return {"id": data["pullRequestId"], "title": data.get("title", ""), "url": repo.web(f"pullrequest/{data['pullRequestId']}")}


def find_pr(repo: Repo, source: str, target: str) -> dict | None:
    """The active pull request from source into target, if there is one."""
    q = urllib.parse.urlencode({
        "searchCriteria.sourceRefName": f"refs/heads/{source}",
        "searchCriteria.targetRefName": f"refs/heads/{target}",
        "searchCriteria.status": "active",
    })
    found = _request(repo, "GET", repo.api(f"pullrequests?{q}&{API}")).get("value") or []
    return _pr(repo, found[0]) if found else None


def create_pr(repo: Repo, source: str, target: str, title: str, description: str,
              reviewers: list[dict] | None = None) -> dict:
    data = _request(repo, "POST", repo.api(f"pullrequests?{API}"), {
        "sourceRefName": f"refs/heads/{source}",
        "targetRefName": f"refs/heads/{target}",
        "title": title[:TITLE_MAX],
        "description": description[:DESCRIPTION_MAX],
        "reviewers": [{"id": r["id"], "isRequired": True} for r in reviewers or []],
    })
    return _pr(repo, data)


# ---------- required reviewers ----------
# The token Git Credential Manager keeps is scoped to code, so it can't search the people directory.
# Reviewers are matched instead against everyone who created or reviewed a pull request anywhere in
# the organization (the IDs come with those pull requests), so someone who has only worked in another
# project is still found, e.g. for a new project with few pull requests yet.

def people(repo: Repo) -> list[dict]:
    """People and groups seen on the organization's last 1000 pull requests: [{"id", "name", "email"}]."""
    q = urllib.parse.urlencode({"searchCriteria.status": "all", "$top": "1000"})
    found: dict[str, dict] = {}
    for pr in _request(repo, "GET", f"{repo.base}/_apis/git/pullrequests?{q}&{API}").get("value") or []:
        for p in [pr.get("createdBy") or {}] + (pr.get("reviewers") or []):
            if p.get("id") and p["id"] not in found:
                found[p["id"]] = {"id": p["id"], "name": p.get("displayName", ""), "email": p.get("uniqueName", "")}
    return list(found.values())


def _vssps(repo: Repo) -> str:
    if repo.host == "dev.azure.com":
        return f"https://vssps.dev.azure.com/{urllib.parse.quote(repo.org)}"
    return f"https://{repo.org}.vssps.visualstudio.com"


def directory_search(repo: Repo, text: str) -> list[dict]:
    """Everyone in the organization's directory matching an email or name (needs the Microsoft sign-in)."""
    q = urllib.parse.urlencode({"searchFilter": "General", "filterValue": text.strip(), "queryMembership": "None"})
    found = _request(repo, "GET", f"{_vssps(repo)}/_apis/identities?{q}&{API}", oauth=True).get("value") or []
    out = []
    for i in found:
        if not i.get("isActive", True) or not i.get("id"):
            continue
        props = i.get("properties") or {}
        mail = ((props.get("Mail") or {}).get("$value") or (props.get("Account") or {}).get("$value") or "")
        out.append({"id": i["id"], "name": i.get("providerDisplayName") or i.get("customDisplayName") or mail, "email": mail})
    return out


def _match(people_list: list[dict], t: str) -> list[dict]:
    return ([p for p in people_list if p["email"].lower() == t]
            or [p for p in people_list if p["name"].lower() == t]
            or [p for p in people_list if t in p["name"].lower() or t in p["email"].lower()])


def resolve_reviewers(repo: Repo, wanted: list[str]) -> list[dict]:
    """Match each reviewer (email or name) to exactly one person; raises AzureError otherwise.

    People already seen on pull requests are matched straight away. Anyone else with access to
    the organization (and so to the project) is looked up in its directory, which needs the
    Microsoft sign-in once."""
    if not wanted:
        return []
    everyone = people(repo)
    out: list[dict] = []
    for text in wanted:
        t = text.strip().lower()
        hits = _match(everyone, t)
        if not hits:
            hits = _match(directory_search(repo, text), t)
        if not hits:
            raise AzureError(f"Couldn't find the reviewer “{text}” in the {repo.org} Azure DevOps organization. "
                             "Use their email exactly as shown in Azure DevOps.")
        if len(hits) > 1:
            names = ", ".join(f"{p['name']} ({p['email']})" for p in hits[:5])
            raise AzureError(f"“{text}” matches more than one person: {names}. Use the email of the one you mean.")
        if hits[0]["id"] not in {r["id"] for r in out}:
            out.append(hits[0])
    return out


def require_reviewers(repo: Repo, pr_id: int, reviewers: list[dict]) -> None:
    """Make these people required reviewers on an existing pull request (votes already cast are kept)."""
    if not reviewers:
        return
    current = {r.get("id"): r for r in _request(repo, "GET", repo.api(f"pullrequests/{pr_id}/reviewers?{API}")).get("value") or []}
    for r in reviewers:
        if current.get(r["id"], {}).get("isRequired"):
            continue
        _request(repo, "PUT", repo.api(f"pullrequests/{pr_id}/reviewers/{r['id']}?{API}"), {"id": r["id"], "isRequired": True})
