"""Sanitize cross-provider message contamination before Anthropic outbound.

Two helpers:
- ``sanitize_messages_for_claude`` — strips foreign keys and signature-less
  ``thinking`` blocks; injects a placeholder when an assistant message is
  fully emptied (Claude rejects empty content).
- ``fix_orphaned_tool_uses`` — injects synthetic error ``tool_result`` blocks
  for any ``tool_use`` lacking a matching ``tool_result``. Mutates in place.

Run both, in order, on the Anthropic-shape dict list AFTER canonical→Anthropic
translation, BEFORE the SDK call.

Ported from Zeemon ``providers/_anthropic_sanitize.py`` (itself grafted from
Soren). Dropped ``shrink_oversized_images`` and its Pillow dependency —
toolforge has no image ingestion yet; an oversized base64 image would 400 at
the API. Revisit if the orchestrator gains vision input.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_FOREIGN_TOOL_USE_KEYS = {"_gemini_thought_signature"}


def sanitize_messages_for_claude(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip non-Anthropic fields from content blocks before sending to Claude."""
    cleaned: list[dict[str, Any]] = []
    for msg_idx, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            cleaned.append(msg)
            continue
        new_content: list[Any] = []
        stripped_types: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue
            btype = block.get("type")
            if btype == "text" and not block.get("text", "").strip():
                stripped_types.append("text(whitespace)")
                continue
            if btype == "thinking" and not block.get("signature"):
                stripped_types.append("thinking(unsigned)")
                continue
            extra = _FOREIGN_TOOL_USE_KEYS & block.keys()
            if extra:
                block = {k: v for k, v in block.items() if k not in _FOREIGN_TOOL_USE_KEYS}
            new_content.append(block)
        if not new_content and msg.get("role") == "assistant":
            logger.warning(
                "sanitize.empty_assistant_patched message_index=%d stripped=%s original_blocks=%d",
                msg_idx,
                stripped_types,
                len(content),
            )
            new_content = [{"type": "text", "text": "..."}]
        cleaned.append({**msg, "content": new_content})
    return cleaned


def fix_orphaned_tool_uses(messages: list[dict[str, Any]]) -> bool:
    """Inject synthetic tool_result blocks for orphaned tool_use calls."""
    repaired = False
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") != "assistant":
            i += 1
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            i += 1
            continue
        tool_use_ids = {
            b["id"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        }
        if not tool_use_ids:
            i += 1
            continue
        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        fulfilled: set[str] = set()
        if next_msg and next_msg.get("role") == "user":
            next_content = next_msg.get("content")
            if isinstance(next_content, list):
                for b in next_content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tid = b.get("tool_use_id")
                        if tid:
                            fulfilled.add(tid)
        orphans = tool_use_ids - fulfilled
        if not orphans:
            i += 1
            continue
        synthetic = [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": "[Error: tool execution was interrupted]",
                "is_error": True,
            }
            for tid in sorted(orphans)
        ]
        if (
            next_msg
            and next_msg.get("role") == "user"
            and isinstance(next_msg.get("content"), list)
        ):
            next_msg["content"] = synthetic + list(next_msg["content"])
        else:
            messages.insert(i + 1, {"role": "user", "content": synthetic})
        repaired = True
        i += 2
    return repaired
