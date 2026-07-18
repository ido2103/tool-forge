"""Safety-envelope tests — TRUSTED vs UNVERIFIED wrapping shape."""

from __future__ import annotations

from toolforge.registry import wrap_tool_result


def test_trusted_wrap_shape() -> None:
    out = wrap_tool_result(tool="run_bash", content="hello", trust="TRUSTED")
    assert out.startswith('<tool_result tool="run_bash" trust="TRUSTED">')
    assert out.endswith("</tool_result>")
    assert "hello" in out
    assert "prompt_injection_warning" not in out
    assert "external_content" not in out


def test_unverified_wrap_shape() -> None:
    out = wrap_tool_result(tool="scrape", content="hello", trust="UNVERIFIED")
    assert 'trust="UNVERIFIED"' in out
    assert "<prompt_injection_warning>" in out
    assert "<external_content>\nhello\n</external_content>" in out
    assert out.endswith("</tool_result>")
