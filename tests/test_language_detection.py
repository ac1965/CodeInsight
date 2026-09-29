from __future__ import annotations

from pathlib import Path

from codeinsight.analysis.language import detect_language
from codeinsight.domain import Language


def test_detects_c_files() -> None:
    assert detect_language(Path("foo.c")) == Language.C
    assert detect_language(Path("foo.h")) == Language.C


def test_detects_python_files() -> None:
    assert detect_language(Path("foo.py")) == Language.PYTHON
    assert detect_language(Path("foo.pyi")) == Language.PYTHON


def test_unknown_extension_is_unknown() -> None:
    assert detect_language(Path("foo.rs")) == Language.UNKNOWN
