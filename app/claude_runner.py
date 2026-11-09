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
