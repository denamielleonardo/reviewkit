"""Runs reviews through the developer's own Claude login using claude-agent-sdk.

The SDK drives the bundled Claude Code CLI. Each developer signs in once
(`claude auth login`) or pastes their own Anthropic API key in Settings.
"""
import json
import os
import subprocess
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)
from claude_agent_sdk.types import StreamEvent, ToolUseBlock

from . import settings
from .paths import bundled_cli
from .skills import REVIEWS, skill_prompt

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Code reviews may read the repo and run read-only git commands; nothing else.
CODE_TOOLS = ["Read", "Grep", "Glob", "Bash"]
CODE_ALLOWED = ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]

BASE_INSTRUCTIONS = (
    "You are running inside ReviewKit, Eclectus Technologies' internal review tool. "
    "You are read-only: never try to modify files, run builds, or run commands other than the "
    "read-only git commands you are allowed. When you have finished investigating, your final "
    "message must be ONLY the finished review in Markdown, following the required structure "
    "below exactly. No preamble such as 'Here is the review' and no closing remarks."
)


def _env() -> dict[str, str]:
    env: dict[str, str] = {}
    key = settings.load().get("api_key", "").strip()
    if key:
        env["ANTHROPIC_API_KEY"] = key
    return env


def _cli() -> str:
    cli = bundled_cli()
    if not cli:
        raise RuntimeError("The bundled Claude CLI is missing from this ReviewKit build.")
    return str(cli)


def auth_status() -> dict:
    """`claude auth status --json`, or the API-key mode if one is configured."""
    if settings.load().get("api_key", "").strip():
        return {"loggedIn": True, "authMethod": "api_key", "label": "Anthropic API key (from Settings)"}
    try:
        p = subprocess.run(
            [_cli(), "auth", "status", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=NO_WINDOW, env={**os.environ, **_env()},
        )
        data = json.loads(p.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        return {"loggedIn": False, "error": str(e)}
    keep = ("loggedIn", "authMethod", "email", "orgName", "subscriptionType", "loginExpiresAt")
    return {k: data.get(k) for k in keep if k in data}


_LOGIN: dict = {"proc": None}


def start_login() -> None:
    """Open a console window running the Claude sign-in flow (opens the browser)."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    _LOGIN["proc"] = subprocess.Popen([_cli(), "auth", "login"], creationflags=flags)


def login_running() -> bool:
    p = _LOGIN["proc"]
    return p is not None and p.poll() is None


def logout() -> None:
    subprocess.run([_cli(), "auth", "logout"], capture_output=True, timeout=30, creationflags=NO_WINDOW)


def _options(system: str, *, cwd: str | None, tools: list[str], allowed: list[str], max_turns: int) -> ClaudeAgentOptions:
    model = settings.load().get("model", "").strip() or None
    return ClaudeAgentOptions(
        system_prompt=system,
        tools=tools,
        allowed_tools=allowed,
        permission_mode="dontAsk",      # anything not in allowed_tools is denied
        setting_sources=[],             # ignore the dev's own CLAUDE.md / skills / hooks
        cwd=cwd,
        max_turns=max_turns,
        model=model,
        cli_path=_cli(),
        env=_env(),
        include_partial_messages=True,
    )


def _short(text: str, cwd: str | None) -> str:
    if cwd:
        for variant in {cwd, cwd.replace("\\", "/"), cwd.replace("/", "\\")}:
            text = text.replace(variant + "\\", "").replace(variant + "/", "").replace(f'cd "{variant}" && ', "")
    return text if len(text) <= 160 else text[:157] + "…"


def _describe_tool(block: ToolUseBlock, cwd: str | None = None) -> str:
    return _short(_describe_raw(block), cwd)


def _describe_raw(block: ToolUseBlock) -> str:
    i = block.input or {}
    if block.name == "Read":
        return f"Reading {i.get('file_path', '')}"
    if block.name == "Grep":
        return f"Searching for “{i.get('pattern', '')}”"
    if block.name == "Glob":
        return f"Listing {i.get('pattern', '')}"
    if block.name == "Bash":
        return f"$ {i.get('command', '')}"
    return block.name


def _system_prompt(review_ids: list[str]) -> str:
    if len(review_ids) == 1:
        rid = review_ids[0]
        label = REVIEWS[rid]["label"]
        display = (
            f"\n\nIn ReviewKit this review type is called \"{label}\". Never show the internal skill name "
            f"({REVIEWS[rid]['skill']}) in the review; wherever the structure below asks for a skill "
            f"or reviewer identifier, write \"{label}\" instead."
        )
        return BASE_INSTRUCTIONS + display + "\n\n" + skill_prompt(rid)
    labels = [REVIEWS[r]["label"] for r in review_ids]
    parts = [
        BASE_INSTRUCTIONS,
        "This change touches more than one kind of code, so the final message contains one review per kind, "
        f"in this order: {', '.join(labels)}. Start each with a level-1 heading \"# <kind> review\" "
        "(e.g. \"# .NET review\"), review ONLY the files listed for that kind in the prompt using that kind's "
        "rules below, follow that kind's required structure, and separate the reviews with a horizontal rule (---). "
        "Never show internal skill names; wherever a structure asks for a skill or reviewer identifier, write the "
        "kind's name instead.",
    ]
    for rid, label in zip(review_ids, labels):
        parts.append(f"# Rules for the {label} review\n\n{skill_prompt(rid)}")
    return "\n\n".join(parts)


async def run(review_id: str | list[str], prompt: str, *, cwd: str | None = None, code: bool = True) -> AsyncIterator[dict]:
    """Yields UI events: text (live text of the current message), tool, done, error."""
    review_ids = [review_id] if isinstance(review_id, str) else list(review_id)
    opts = _options(
        _system_prompt(review_ids), cwd=cwd,
        tools=CODE_TOOLS if code else [],
        allowed=CODE_ALLOWED if code else [],
        max_turns=(60 + 30 * (len(review_ids) - 1)) if code else 3,
    )
    try:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                if ev.get("type") == "message_start" and not msg.parent_tool_use_id:
                    yield {"t": "reset"}
                elif ev.get("type") == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta" and not msg.parent_tool_use_id:
                        yield {"t": "text", "v": d.get("text", "")}
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        yield {"t": "tool", "v": _describe_tool(block, cwd)}
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    detail = "; ".join(msg.errors or []) or msg.result or msg.subtype
                    yield {"t": "error", "v": f"Claude stopped: {detail}"}
                    return
                yield {
                    "t": "done",
                    "markdown": (msg.result or "").strip(),
                    "seconds": round((msg.duration_ms or 0) / 1000),
                    "turns": msg.num_turns,
                    "cost": msg.total_cost_usd,
                }
                return
    except Exception as e:  # noqa: BLE001
        text = str(e)
        if "auth" in text.lower() or "login" in text.lower() or "401" in text:
            text += "\n\nYou may be signed out. Open Settings and click Sign in."
        yield {"t": "error", "v": text}


EDIT_SYSTEM = """You are editing an Eclectus Technologies Technical Design Document to resolve specific review points that the author has accepted.

Strict rules:
1. Address ONLY the review points listed under "Accepted review points". Never change anything for any other reason: no fixing typos, rewording, reformatting, renumbering or "improving" unrelated text, even if you notice problems.
2. Follow the document's existing template and structure. Put new content in the section where it belongs, matching how neighbouring content is written (tone, tense, numbering scheme, bullet vs. table, heading levels). Do not add new top-level sections unless a point explicitly requires one, and if it does, number it to match the document's scheme.
3. Make the smallest edit that fully resolves each point. Prefer extending an existing paragraph, cell or table over adding new blocks. Replace text only when the existing text itself is wrong. Never delete content unless a point requires it.
4. Only use "style" values from the "Styles used in this document" list. Omit "style" to copy the formatting of the target paragraph.
5. Target only IDs that appear in the document outline. Every edit must name the single accepted point_id it resolves.
6. Write the final document text itself, as the author would. No reviewer comments, notes to the author, "TBD" placeholders, or mentions of the review.
7. If a point cannot be resolved without information only the author has (e.g. real SLA numbers, owner names), do not invent facts: put it in "skipped" with a short reason.

Operations:
- replace: replace the full text of a paragraph (P#) or table cell (T#.R#.C#) with "text" (include the unchanged parts of that paragraph).
- insert_after: insert one or more new paragraphs ("text"; one paragraph per line) after a paragraph, table or cell. Optional "style".
- add_row: add a table row after a row (T#.R#) or at the end of a table (T#); "cells" lists the cell texts in column order.
- delete: delete a paragraph (P#). Only when a point requires removing it."""

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point_id": {"type": "string"},
                    "op": {"type": "string", "enum": ["replace", "insert_after", "add_row", "delete"]},
                    "target": {"type": "string"},
                    "text": {"type": "string"},
                    "style": {"type": "string"},
                    "cells": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string", "description": "One short sentence: what this edit changes."},
                },
                "required": ["point_id", "op", "target", "summary"],
            },
        },
        "skipped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"point_id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["point_id", "reason"],
            },
        },
    },
    "required": ["edits", "skipped"],
}


async def plan_edits(doc_name: str, outline: str, styles: list[str], points: list[dict]) -> dict:
    """Ask Claude for tracked-change edits that resolve only the accepted points."""
    accepted = "\n".join(
        f"- {p['id']} [{p.get('severity', '')}] ({p.get('section', '')}) {p.get('title', '')}: {p.get('change', '')}"
        for p in points
    )
    prompt = (
        f"Document: {doc_name}\n\n"
        f"## Styles used in this document\n{', '.join(styles) or 'Normal'}\n\n"
        f"## Accepted review points\n{accepted}\n\n"
        f"## Document outline (IDs in brackets)\n{outline}"
    )
    opts = _options(EDIT_SYSTEM, cwd=None, tools=[], allowed=[], max_turns=4)
    opts.include_partial_messages = False
    opts.output_format = {"type": "json_schema", "schema": EDIT_SCHEMA}
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, ResultMessage):
            if msg.is_error:
                raise RuntimeError("; ".join(msg.errors or []) or msg.result or msg.subtype)
            data = msg.structured_output
            if data is None and msg.result:
                data = json.loads(msg.result)
            if not isinstance(data, dict):
                raise RuntimeError("Claude did not return an edit plan.")
            return data
    raise RuntimeError("No response from Claude.")


async def test_connection() -> dict:
    opts = _options("Reply with exactly: OK", cwd=None, tools=[], allowed=[], max_turns=1)
    opts.include_partial_messages = False
    try:
        async for msg in query(prompt="ping", options=opts):
            if isinstance(msg, ResultMessage):
                if msg.is_error:
                    return {"ok": False, "error": "; ".join(msg.errors or []) or msg.result or msg.subtype}
                return {"ok": True, "reply": (msg.result or "").strip()[:40]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "No response from Claude."}


# ---------- applying code review points ----------

APPLY_TOOLS = ["Read", "Grep", "Glob", "Edit", "MultiEdit", "Write"]  # no Bash: cannot run git, commit or push

CODE_APPLY_SYSTEM = """You are applying accepted code review points to a developer's local clone, inside ReviewKit (Eclectus Technologies).

Strict rules:
1. Change ONLY what is needed to resolve the review points listed under "Accepted review points". Do not fix, refactor, reformat, rename or "improve" anything else, even if you notice problems. No drive-by changes.
2. Follow the code base's existing patterns, naming and formatting. Read the surrounding code (and related files) before editing so the change fits.
3. Make the smallest change that fully resolves each point. Keep edits inside this repository. Never touch the .git folder, build output, lock files or generated files unless a point is explicitly about them.
4. You cannot run commands, builds, tests or git. Do not commit, stage or push; the developer reviews your changes themselves.
5. If a point cannot be resolved safely without information only the developer has, or the code it refers to no longer exists, do not guess: mark it "skipped" with a short reason.
6. Every point listed must appear exactly once in "results"."""

CODE_APPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["applied", "skipped"]},
                    "summary": {"type": "string", "description": "One sentence: what was changed, or why it was skipped."},
                    "files": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["point_id", "status", "summary"],
            },
        },
    },
    "required": ["results"],
}


def _write_guard(root: str):
    """PreToolUse hook: deny any write outside the repository or inside .git."""
    from pathlib import Path

    base = Path(root).resolve()

    async def guard(input_data, tool_use_id, context):
        tool = input_data.get("tool_name", "")
        if tool not in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
            return {}
        raw = (input_data.get("tool_input") or {}).get("file_path") or (input_data.get("tool_input") or {}).get("notebook_path") or ""
        reason = ""
        try:
            target = (base / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            rel = target.relative_to(base)
            if rel.parts and rel.parts[0].lower() == ".git":
                reason = "Writing inside .git is not allowed."
        except (ValueError, OSError):
            reason = f"{raw} is outside the repository; only files inside {base} may be changed."
        if not reason:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    return guard


async def apply_code_points(root: str, review_ids: list[str], points: list[dict], branch: str) -> AsyncIterator[dict]:
    """Edit the working tree to resolve the given points. Yields tool events, then {"t": "result", ...}."""
    from claude_agent_sdk import HookMatcher

    accepted = "\n".join(
        f"- {p['id']} [{p.get('severity', '')}] {p.get('file', '')}"
        f"{(':' + str(p['line'])) if p.get('line') else ''} | {p.get('title', '')}: {p.get('change', '')}"
        for p in points
    )
    rules = "\n\n".join(f"# {REVIEWS[r]['label']} review rules (for context)\n\n{skill_prompt(r)}" for r in review_ids)
    prompt = (
        f"Branch checked out: {branch}\n\n## Accepted review points\n{accepted}\n\n"
        "Apply these points to the files in this repository now, then report the result for each point."
    )
    opts = _options(CODE_APPLY_SYSTEM + "\n\n" + rules, cwd=root, tools=APPLY_TOOLS, allowed=APPLY_TOOLS,
                    max_turns=40 + 10 * len(points))
    opts.include_partial_messages = False
    opts.output_format = {"type": "json_schema", "schema": CODE_APPLY_SCHEMA}
    opts.hooks = {"PreToolUse": [HookMatcher(matcher="Edit|MultiEdit|Write|NotebookEdit", hooks=[_write_guard(root)])]}
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    i = block.input or {}
                    if block.name in ("Edit", "MultiEdit", "Write"):
                        yield {"t": "tool", "v": _short(f"Editing {i.get('file_path', '')}", root)}
                    else:
                        yield {"t": "tool", "v": _describe_tool(block, root)}
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                raise RuntimeError("; ".join(msg.errors or []) or msg.result or msg.subtype)
            data = msg.structured_output
            if data is None and msg.result:
                try:
                    data = json.loads(msg.result)
                except json.JSONDecodeError:
                    data = None
            yield {"t": "result", "data": data if isinstance(data, dict) else {"results": []}}
            return
    raise RuntimeError("No response from Claude.")
