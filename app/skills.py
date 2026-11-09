"""Embedded review skills (copied from the team's synced Claude skills)."""
import re
from pathlib import Path

from .paths import SKILLS_DIR

REVIEWS = {
    "capataz": {"label": ".NET", "skill": "capataz-api-reviewer", "kind": "code"},
    "angular": {"label": "Angular", "skill": "angular-code-reviewer", "kind": "code"},
    "ionic": {"label": "Ionic", "skill": "ionic-framework-code-reviewer", "kind": "code"},
    "td": {"label": "Technical Design", "skill": "technical-design-reviewer", "kind": "td"},
}


def _strip_frontmatter(text: str) -> str:
    text = text.replace("\r\n", "\n")
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S).strip()


def skill_prompt(review_id: str) -> str:
    """SKILL.md body plus any example files in the skill folder."""
    folder: Path = SKILLS_DIR / REVIEWS[review_id]["skill"]
    parts = [_strip_frontmatter((folder / "SKILL.md").read_text(encoding="utf-8"))]
    examples = folder / "examples"
    if examples.is_dir():
        for ex in sorted(examples.glob("*.md")):
            body = ex.read_text(encoding="utf-8").strip()
            parts.append(f"## Reference example ({ex.name})\n\n{body}")
    return "\n\n".join(parts)
