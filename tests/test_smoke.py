"""Smoke test: the package and all subsystem packages import cleanly."""

import importlib

import pytest

SUBSYSTEMS = ["orchestrator", "forge", "registry", "skills", "sandbox", "evals", "providers"]


def test_package_imports() -> None:
    import toolforge  # noqa: F401


@pytest.mark.parametrize("subsystem", SUBSYSTEMS)
def test_subsystem_imports(subsystem: str) -> None:
    importlib.import_module(f"toolforge.{subsystem}")
