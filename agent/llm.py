"""
agent/llm.py
-------------
Thin LLM provider abstraction.

Reads LLM_PROVIDER env var ("openrouter", "openai", or "anthropic").
If not set, auto-detects based on available API keys in order:
  1. OPENROUTER_API_KEY  (access to gpt-4o-mini, llama-3.3-70b, etc.)
  2. OPENAI_API_KEY      (OpenAI platform direct)
  3. ANTHROPIC_API_KEY   (Anthropic platform direct)

Exposes a single function:
    chat(messages, tools=None) -> (content: str, tool_calls: list)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("aegis.llm")

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")


def _get_provider() -> str:
    provider = os.environ.get("LLM_PROVIDER", "").lower()
    if provider in ("openrouter", "openai", "anthropic"):
        return provider
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    raise EnvironmentError(
        "No LLM provider configured. Set OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY."
    )


# ── OpenRouter ────────────────────────────────────────────────────────────────

def _openrouter_chat(
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
) -> Tuple[Optional[str], List[Dict]]:
    from openai import OpenAI  # type: ignore

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
        timeout=30.0,
    )
    kwargs: Dict[str, Any] = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    response = client.chat.completions.create(**kwargs)
    msg = response.choices[0].message
    content = msg.content or ""
    tool_calls = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                }
            )
    return content, tool_calls


# ── OpenAI ────────────────────────────────────────────────────────────────────

def _openai_chat(
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
) -> Tuple[Optional[str], List[Dict]]:
    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=30.0)
    kwargs: Dict[str, Any] = {
        "model": OPENAI_MODEL,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    response = client.chat.completions.create(**kwargs)
    msg = response.choices[0].message
    content = msg.content or ""
    tool_calls = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                }
            )
    return content, tool_calls


# ── Anthropic ─────────────────────────────────────────────────────────────────

def _anthropic_chat(
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
) -> Tuple[Optional[str], List[Dict]]:
    import anthropic as ant  # type: ignore

    client = ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=30.0)

    # Anthropic uses a system param separate from messages
    system = ""
    filtered_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            filtered_msgs.append(m)

    # Convert OpenAI-style tool schemas to Anthropic format
    ant_tools = []
    if tools:
        for t in tools:
            fn = t.get("function", t)
            ant_tools.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )

    kwargs: Dict[str, Any] = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "messages": filtered_msgs,
    }
    if system:
        kwargs["system"] = system
    if ant_tools:
        kwargs["tools"] = ant_tools

    response = client.messages.create(**kwargs)
    content = ""
    tool_calls = []
    for block in response.content:
        if block.type == "text":
            content = block.text
        elif block.type == "tool_use":
            tool_calls.append(
                {
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                }
            )
    return content, tool_calls


# ── Public interface ──────────────────────────────────────────────────────────

def chat(
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
) -> Tuple[Optional[str], List[Dict]]:
    """
    Call the configured LLM.

    Returns:
        content    : the assistant's text reply (may be empty if only tool calls)
        tool_calls : list of {"id", "name", "arguments"} dicts
    """
    provider = _get_provider()
    if provider == "openrouter":
        return _openrouter_chat(messages, tools)
    if provider == "openai":
        return _openai_chat(messages, tools)
    return _anthropic_chat(messages, tools)
