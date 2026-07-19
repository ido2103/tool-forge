"""Textual TUI — the rich interactive surface over the same host seams as the REPL.

The app is a *host* in the `bootstrap.build_host` sense: it injects its own
hook handlers and (later) an ``ask_user`` modal callback, and renders the
orchestrator's streaming callbacks and hook events. Nothing here is imported by
the core subsystems — the dependency points one way, TUI → orchestrator.
"""

from toolforge.tui.app import ToolforgeApp, main

__all__ = ["ToolforgeApp", "main"]
