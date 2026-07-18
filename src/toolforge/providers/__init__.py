"""Model clients — the single model-I/O layer under the orchestrator and forge.

Two adapters implement the :class:`~toolforge.providers.base.ProviderClient`
protocol:

- :class:`~toolforge.providers.anthropic.AnthropicClient` — the orchestrator's
  frontier model (Anthropic Messages API; api-key or OAuth auth).
- :class:`~toolforge.providers.openai_compat.OpenAICompatClient` — the forge
  worker's local model (any OpenAI-compatible Chat Completions server:
  vLLM, llama.cpp, LM Studio, Ollama).

Both consume/produce the canonical :class:`~toolforge.providers.messages.Message`
types and normalize stop reasons to the Anthropic vocabulary, so the agent
loops never touch provider-specific wire formats. Ported from Zeemon's
provider layer; see docs/providers.md for provenance and what was dropped.
"""

from toolforge.providers.anthropic import AnthropicClient
from toolforge.providers.base import (
    AuthMode,
    MessageEnd,
    PermanentProviderError,
    ProviderClient,
    ProviderError,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
    TransientProviderError,
    drain_send,
    is_transient_status,
)
from toolforge.providers.messages import (
    ContentBlock,
    DocumentBlock,
    ImageBlock,
    Message,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from toolforge.providers.openai_compat import OpenAICompatClient
from toolforge.providers.usage import UsageEvent, UsageHook, log_usage

__all__ = [
    "AnthropicClient",
    "AuthMode",
    "ContentBlock",
    "DocumentBlock",
    "ImageBlock",
    "Message",
    "MessageEnd",
    "OpenAICompatClient",
    "PermanentProviderError",
    "ProviderClient",
    "ProviderError",
    "ProviderEvent",
    "RedactedThinkingBlock",
    "TextBlock",
    "TextDelta",
    "ThinkingBlock",
    "ThinkingDelta",
    "ToolResultBlock",
    "ToolUseBlock",
    "ToolUseDelta",
    "ToolUseEnd",
    "ToolUseStart",
    "TransientProviderError",
    "Usage",
    "UsageEvent",
    "UsageHook",
    "drain_send",
    "is_transient_status",
    "log_usage",
]
