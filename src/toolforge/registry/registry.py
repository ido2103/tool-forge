"""The instance tool registry — the growing toolbox.

Unlike Zeemon's module-global decorator registry, this is an explicit object the
orchestrator holds. That matters because toolforge's whole premise is a toolbox
that grows *mid-task*: when the forge finishes building a tool, it calls
``register(...)`` on the live registry, and because the loop re-reads
``get_schemas()`` at the top of every iteration, the new tool is visible to the
model on the very next turn — no payload surgery, no restart.
"""

from __future__ import annotations

from typing import Any

from toolforge.registry.safety import wrap_tool_result
from toolforge.registry.types import RegisteredTool, ToolContext, ToolResult, Trust


class ToolRegistry:
    """Holds :class:`RegisteredTool`s and the per-turn :class:`ToolContext`."""

    def __init__(self, context: ToolContext | None = None) -> None:
        self.context = context if context is not None else ToolContext()
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool, *, replace: bool = False) -> None:
        """Add a tool. Raises ``ValueError`` on a name clash unless ``replace``."""
        if tool.name in self._tools and not replace:
            raise ValueError(
                f"tool {tool.name!r} already registered; pass replace=True to overwrite"
            )
        self._tools[tool.name] = tool

    def replace(self, tool: RegisteredTool) -> None:
        """Overwrite an existing tool (or register it if absent)."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        del self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def trust_for(self, name: str) -> Trust:
        """Trust level for *name*, for wrapping output the tool never got to return.

        Unknown names fall back to ``TRUSTED`` because the only content that can
        exist for an unregistered tool is a harness-generated error string — there
        is no external payload to quarantine.
        """
        tool = self._tools.get(name)
        return tool.trust if tool is not None else "TRUSTED"

    def get_schemas(self) -> list[dict[str, Any]]:
        """The Anthropic-shape tool defs for the model.

        A fresh list built on every call — never a cached snapshot — so a tool
        registered mid-task is picked up on the next iteration.
        """
        return [t.schema for t in self._tools.values()]

    async def execute(self, name: str, input: dict[str, Any]) -> ToolResult:
        """Run a tool's handler and wrap its string output in the safety envelope.

        Raises ``KeyError`` if ``name`` is not registered — the loop turns that
        into an ``is_error`` result so a hallucinated tool name never aborts the run.
        List (multimodal) content passes through unwrapped.
        """
        tool = self._tools[name]
        result = await tool.handler(input, self.context)
        if isinstance(result.content, str):
            result.content = wrap_tool_result(
                tool=name,
                content=result.content,
                trust=tool.trust,
            )
        return result
