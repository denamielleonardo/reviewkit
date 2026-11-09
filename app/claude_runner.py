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
from .skills import skill_prompt

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


def start_login() -> None:
    """Open a console window running the Claude sign-in flow (opens the browser)."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen([_cli(), "auth", "login"], creationflags=flags)


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


async def run(review_id: str, prompt: str, *, cwd: str | None = None, code: bool = True) -> AsyncIterator[dict]:
    """Yields UI events: text (live text of the current message), tool, done, error."""
    system = BASE_INSTRUCTIONS + "\n\n" + skill_prompt(review_id)
    opts = _options(
        system, cwd=cwd,
        tools=CODE_TOOLS if code else [],
        allowed=CODE_ALLOWED if code else [],
        max_turns=60 if code else 3,
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
