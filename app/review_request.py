"""Pull request title and the "[For Code Review]" email that asks a reviewer to look at it.

Title:   FEATURE: SP0427 - Fill the company location from the map pin
         (type from the branch prefix, ticket from the branch name, description written by Claude)
Email:   opened as a draft in Outlook on the web through its compose link, so it is sent from the
         developer's own mailbox and Outlook adds their own signature. ReviewKit never sends it.
"""
import re
import urllib.parse

SUBJECT_PREFIX = "[For Code Review]"

# Shown in the Required reviewers and Send to dropdowns. People added with "Other…" are stored per PC
# (settings "pr_people") and listed after these.
PEOPLE = [
    {"email": "denamiel.c.leonardo@eclectustech.net", "name": "Den Amiel Leonardo"},
    {"email": "johncristophere.j.carlos@eclectustech.net", "name": "John Cristophere Carlos"},
    {"email": "christiandave.j.carlos@eclectustech.net", "name": "Christian Dave Carlos"},
]


def people(custom: list[dict]) -> list[dict]:
    """Built-in people, then the ones this PC added (no duplicates); custom ones can be removed."""
    out, seen = [], set()
    for p, is_custom in [(p, False) for p in PEOPLE] + [(p, True) for p in custom or []]:
        email = str(p.get("email", "")).strip()
        if email and email.lower() not in seen:
            seen.add(email.lower())
            out.append({"email": email, "name": str(p.get("name", "")).strip(), "custom": is_custom})
    return out
OUTLOOK_COMPOSE = "https://outlook.cloud.microsoft/mail/deeplink/compose"

_TYPES = {"feature": "FEATURE", "feat": "FEATURE", "fix": "FIX", "bugfix": "FIX", "bug": "FIX",
          "hotfix": "HOTFIX", "chore": "CHORE", "refactor": "REFACTOR", "release": "RELEASE"}
_TICKET = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,5}\d{2,6})(?![A-Za-z0-9])")
_EMAIL = re.compile(r"^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$")


def branch_parts(branch: str) -> tuple[str, str, str]:
    """('FEATURE', 'SP0427', 'company location map pin') from 'feature/sp0427-company-location-map-pin'."""
    prefix, _, rest = branch.strip().removeprefix("origin/").rpartition("/")
    kind = _TYPES.get(prefix.split("/")[0].lower(), prefix.split("/")[0].upper()) if prefix else "FEATURE"
    m = _TICKET.search(rest)
    ticket = m.group(1).upper() if m else ""
    words = _TICKET.sub(" ", rest) if m else rest
    words = re.sub(r"[-_.\s]+", " ", words).strip()
    return kind, ticket, words


def pr_title(branch: str, summary: str = "") -> str:
    kind, ticket, words = branch_parts(branch)
    text = (summary or words or branch).strip().rstrip(".")
    text = text[:1].upper() + text[1:]
    return f"{kind}: " + (f"{ticket} - " if ticket else "") + text


def pr_description(commits: list[str], limit: int = 4000) -> str:
    """One '- <commit message>' row per commit, kept under Azure DevOps' description limit."""
    rows: list[str] = []
    for i, msg in enumerate(commits):
        row = f"- {msg}"
        more = len(commits) - i
        if sum(len(r) + 1 for r in rows) + len(row) + 40 > limit:
            rows.append(f"- … and {more} more commit{'s' if more != 1 else ''}")
            break
        rows.append(row)
    return "\n".join(rows)


def valid_email(addr: str) -> bool:
    return bool(_EMAIL.match(addr.strip()))


def split_list(text: str) -> list[str]:
    """Free text with several entries separated by ';', ',' or new lines."""
    return [p.strip() for p in re.split(r"[;,\n]", text or "") if p.strip()]


def bad_addresses(text: str) -> list[str]:
    return [a for a in split_list(text) if not valid_email(a)]


def email(pr: dict, greeting: str) -> tuple[str, str]:
    """(subject, plain-text body) for the review request."""
    line = f"Pull Request {pr['id']}: {pr['title']}"
    subject = f"{SUBJECT_PREFIX}: {line}"
    body = (f"Hi {greeting.strip() or 'there'},\n\nGood day!\n\n"
            f"I am kindly requesting a Code Review for the following:\n{line}\n{pr['url']}\n\nThank you!")
    return subject, body


def compose_url(to: str, pr: dict, greeting: str) -> str:
    subject, body = email(pr, greeting)
    q = urllib.parse.urlencode({"to": ";".join(split_list(to)), "subject": subject, "body": body},
                               quote_via=urllib.parse.quote)
    return f"{OUTLOOK_COMPOSE}?{q}"
