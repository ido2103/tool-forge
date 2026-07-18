"""The orchestrator-facing forge tools: ``forge_tool`` and ``register_tool``.

Two composable primitives instead of one auto-registering mega-tool (per the
granularity principle): ``forge_tool`` builds a *candidate* and never touches
the registry; ``register_tool`` promotes a candidate only after the orchestrator
has run its own holdout check. The gap between the two calls structurally
enforces the spec's rule that green tests alone never register a tool — the
forge grades its own homework, so an independent verification must happen in
between.

Both handlers fully validate their input (the validation survives into the real
implementation) and currently return a guided not-implemented error: the
internal build loop (test author + worker) is a future slice.
"""

from __future__ import annotations

import re
from typing import Any

from toolforge.forge.candidates import CandidateStore
from toolforge.registry import RegisteredTool, ToolContext, ToolRegistry, ToolResult

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_FORGE_DESCRIPTION = """\
Build a CANDIDATE tool from a spec you author: the forge writes adversarial \
tests from your spec, implements the tool in the sandbox, and returns the \
candidate's code, test report, and workspace file paths. It does NOT register \
anything — the candidate only becomes callable after you independently verify \
it and call register_tool.

Use this only after hitting a genuine capability wall. Do NOT forge when \
existing registered tools — especially a composition of run_bash calls — can do \
the job; check your available tools first and account for that in gap_analysis. \
Do NOT register a candidate on the strength of the forge's own green tests.

How to use it:
- gap_analysis comes first for a reason: writing down what you tried and why \
composition fails is how you catch yourself about to forge something unnecessary.
- You author the tool's contract: name, model-facing description, input_schema, \
and a behavior contract. The candidate's Python function signature is derived \
mechanically from your input_schema — make properties and required exact.
- Tools needing network access must declare allowed_domains; an absent or empty \
list means the tool runs with NO network. Any network access makes the tool's \
future output UNVERIFIED (it will carry the prompt-injection envelope).
- Forging the same name again replaces the previous candidate — use this to \
revise a spec after a failed verification.
- After a successful forge, verify the candidate yourself: exercise the returned \
files against unseen inputs via run_bash and/or review the code, then call \
register_tool with that evidence.
- If this call returns an error, read it before acting — it says whether to \
revise the spec, fall back to existing tools, or report the capability gap to \
the user. Do not retry an identical call after an error."""

_FORGE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "gap_analysis": {
            "type": "string",
            "description": (
                "What you tried, which registered tools you considered, and why no "
                "existing tool or composition of tools (including run_bash scripting) "
                "can close this gap. Be concrete — cite the failed attempts. If you "
                "cannot articulate why composition fails, do not forge."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Name for the new tool. Must match ^[a-zA-Z0-9_-]{1,64}$ and must not "
                "collide with any currently registered tool. Pick a composable-"
                "primitive name (verb_noun like 'fetch_rss'), not a task-specific one."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                "The model-facing description the finished tool will carry in the "
                "registry: one-sentence summary, when to use it and when NOT to, and "
                "any constraints. This is prompt text for your future self — write it "
                "with the same care as this tool's own description."
            ),
        },
        "input_schema": {
            "type": "object",
            "description": (
                "Anthropic-style JSON Schema for the new tool's input: an object with "
                "type 'object', a 'properties' map (each property with a type and a "
                "model-facing description), and a 'required' list. The implementation's "
                "Python signature is derived mechanically from this schema, so it is "
                "the single source of truth for the tool's arguments."
            ),
        },
        "behavior": {
            "type": "string",
            "description": (
                "The behavior contract the implementation and its tests are built "
                "from: expected behavior for normal inputs, edge cases and how each "
                "must be handled, and the error contract (what failures can occur and "
                "what the tool returns for them). Ambiguity here becomes bugs there."
            ),
        },
        "allowed_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Network domains the tool may reach (bare hostnames like "
                "'api.github.com', no scheme or path). Omit or leave empty for a "
                "no-network tool. Declaring any domain forces the tool's output to be "
                "treated as UNVERIFIED. List only what the tool actually needs."
            ),
        },
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "object",
                        "description": "A concrete input object matching input_schema.",
                    },
                    "output": {
                        "type": "string",
                        "description": "The exact output the tool must produce for it.",
                    },
                },
                "required": ["input", "output"],
            },
            "description": (
                "2-3 concrete input->output examples. Strongly recommended whenever "
                "argument semantics or output format are not obvious from the schema "
                "alone — examples anchor both the tests and the implementation."
            ),
        },
    },
    "required": ["gap_analysis", "name", "description", "input_schema", "behavior"],
}

_REGISTER_DESCRIPTION = """\
Promote a previously forged candidate into the live tool registry, making it \
callable from your next turn onward.

Use this only after you have verified the candidate YOURSELF: run its code \
against unseen inputs via run_bash and/or review the returned code against the \
behavior contract. Do NOT register on the strength of the forge's own green \
tests — the forge wrote and passed those tests itself, so they prove nothing \
about spec conformance.

Constraints:
- holdout_evidence comes first for a reason: if you cannot describe the unseen \
cases you ran or the review you did, you have not verified the candidate — go \
verify it.
- The name must exactly match a candidate previously built by forge_tool in \
this session; there is no partial matching.
- Registration is immediate and the registered tool's output will be treated as \
UNVERIFIED. If verification failed, do not register — revise the spec and \
re-forge instead."""

_REGISTER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "holdout_evidence": {
            "type": "string",
            "description": (
                "Your verification record: which unseen inputs (not in the forge's "
                "tests) you ran against the candidate and what they produced, and/or "
                "what your code review checked against the behavior contract. Concrete "
                "commands and observed outputs, not assertions of confidence."
            ),
        },
        "name": {
            "type": "string",
            "description": "The candidate to promote — exactly as passed to forge_tool.",
        },
    },
    "required": ["holdout_evidence", "name"],
}

_FORGE_NOT_IMPLEMENTED = (
    "[forge_tool error: the forge is not yet implemented — your spec was valid but no "
    "tool was built and no candidate was created. Do NOT retry this call; it will fail "
    "identically. Proceed with your existing tools (compose run_bash where possible), "
    "or report to the user that this task needs a capability that cannot be forged yet.]"
)
_REGISTER_NOT_IMPLEMENTED = (
    "[register_tool error: the forge is not yet implemented — no tool was registered. "
    "Do NOT retry this call; it will fail identically. Proceed with your existing "
    "tools, or report to the user that this task needs a capability that cannot be "
    "forged yet.]"
)


def _error(message: str) -> ToolResult:
    return ToolResult(tool_use_id="", content=message, is_error=True)


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def _validate_input_schema(schema: Any) -> str | None:
    """Structural sanity of an orchestrator-authored tool schema; reason or ``None``.

    Deliberately hand-rolled: full JSON-Schema validation would need a new
    dependency, and the API rejects deeper invalidity at registration time anyway.
    """
    if not isinstance(schema, dict):
        return "it is not an object"
    if schema.get("type") != "object":
        return "its 'type' must be \"object\""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return "it must have a 'properties' object"
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            return f"property {prop_name!r} must be a schema object"
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            return "'required' must be a list of property names"
        for entry in required:
            if entry not in properties:
                return f"'required' names undefined property {entry!r}"
    return None


def build_forge_tool(store: CandidateStore, registry: ToolRegistry) -> RegisteredTool:
    """Build ``forge_tool`` bound to *store* and the live *registry*.

    TRUSTED: its output is harness-generated text, never external content.
    """

    async def handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if _blank(inp.get("gap_analysis")):
            return _error(
                "[forge_tool error: 'gap_analysis' must be a non-empty string "
                "explaining why existing tools cannot do this]"
            )
        name = inp.get("name")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            return _error("[forge_tool error: 'name' must match ^[a-zA-Z0-9_-]{1,64}$]")
        if registry.has(name):
            return _error(
                f"[forge_tool error: a tool named {name!r} is already registered; "
                "pick a different name or use the existing tool]"
            )
        if _blank(inp.get("description")):
            return _error("[forge_tool error: 'description' must be a non-empty string]")
        schema_problem = _validate_input_schema(inp.get("input_schema"))
        if schema_problem is not None:
            return _error(
                "[forge_tool error: 'input_schema' must be a JSON Schema object: "
                "type \"object\", a 'properties' dict of {name: schema} entries, and "
                f"an optional 'required' list naming only defined properties — "
                f"{schema_problem}]"
            )
        if _blank(inp.get("behavior")):
            return _error("[forge_tool error: 'behavior' must be a non-empty string]")
        domains = inp.get("allowed_domains")
        if domains is not None:
            valid_domains = isinstance(domains, list) and all(
                isinstance(d, str) and d.strip() and "://" not in d and "/" not in d
                for d in domains
            )
            if not valid_domains:
                return _error(
                    "[forge_tool error: 'allowed_domains' must be a list of bare "
                    'domain names like "api.example.com" (no scheme, no path)]'
                )
        examples = inp.get("examples")
        if examples is not None:
            valid_examples = isinstance(examples, list) and all(
                isinstance(ex, dict)
                and isinstance(ex.get("input"), dict)
                and isinstance(ex.get("output"), str)
                for ex in examples
            )
            if not valid_examples:
                return _error(
                    "[forge_tool error: 'examples' must be a list of "
                    '{"input": {...}, "output": "..."} objects]'
                )

        # Spec is valid; the internal build loop is a future slice. No Candidate
        # is stored — one exists only after a successful build.
        return _error(_FORGE_NOT_IMPLEMENTED)

    return RegisteredTool(
        name="forge_tool",
        description=_FORGE_DESCRIPTION,
        input_schema=_FORGE_INPUT_SCHEMA,
        handler=handler,
        trust="TRUSTED",
    )


def build_register_tool(store: CandidateStore, registry: ToolRegistry) -> RegisteredTool:
    """Build ``register_tool`` bound to *store* and the live *registry*."""

    async def handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if _blank(inp.get("holdout_evidence")):
            return _error(
                "[register_tool error: 'holdout_evidence' must be a non-empty string "
                "describing the unseen cases and/or code review you ran]"
            )
        name = inp.get("name")
        if _blank(name):
            return _error("[register_tool error: 'name' must be a non-empty string]")
        assert isinstance(name, str)
        if not store.has(name):
            return _error(
                f"[register_tool error: no candidate named {name!r} exists; "
                "forge_tool must build it successfully first]"
            )

        return _error(_REGISTER_NOT_IMPLEMENTED)

    return RegisteredTool(
        name="register_tool",
        description=_REGISTER_DESCRIPTION,
        input_schema=_REGISTER_INPUT_SCHEMA,
        handler=handler,
        trust="TRUSTED",
    )
