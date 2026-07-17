"""Registry: the growing toolbox.

Stores each tool as a :class:`RegisteredTool` (wire schema + async handler +
trust level) in an instance :class:`ToolRegistry` the orchestrator holds. The
loop re-reads ``get_schemas()`` every iteration, so a tool the forge registers
mid-task becomes callable on the next turn.

Retrieval-before-forge, spec/test storage, and the v2 curator are future slices;
v0 is the live tool store + the XML safety envelope for tool output.
"""

from toolforge.registry.registry import ToolRegistry
from toolforge.registry.safety import wrap_tool_result
from toolforge.registry.types import (
    RegisteredTool,
    ToolCall,
    ToolContext,
    ToolHandler,
    ToolResult,
    Trust,
)

__all__ = [
    "RegisteredTool",
    "ToolCall",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "Trust",
    "wrap_tool_result",
]
