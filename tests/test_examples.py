"""Every example must at least compile — the cheap guard against signature rot.

Examples can't run in CI (they need live credentials), but a compile pass
catches syntax errors and keeps them honest against refactors; the import-time
surface (names used from infersoft) is additionally checked by linting.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import pytest

EXAMPLES = sorted((Path(__file__).parent.parent / "examples").glob("*.py"))


def test_examples_exist() -> None:
    assert len(EXAMPLES) >= 6


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_compiles(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)
