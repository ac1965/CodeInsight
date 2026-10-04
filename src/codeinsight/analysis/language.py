from __future__ import annotations

from pathlib import Path

from codeinsight.domain import Language

_EXTENSION_TO_LANGUAGE: dict[str, Language] = {
    ".c": Language.C,
    ".h": Language.C,  # C++のヘッダーも `.h` の場合があるが、拡張子だけでは区別できない（C++の解析は、`.hpp`・`.hh`・`.hxx` と、`.cc`・`.cpp`・`.cxx`）
    ".cc": Language.CPP,
    ".cpp": Language.CPP,
    ".cxx": Language.CPP,
    ".c++": Language.CPP,
    ".hpp": Language.CPP,
    ".hh": Language.CPP,
    ".hxx": Language.CPP,
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".el": Language.ELISP,
    ".org": Language.ELISP,  # リテラルプログラミング。emacs-lisp ブロックのみ解析する
}


def detect_language(path: Path) -> Language:
    """拡張子に基づいて対象言語を識別する。

    識別は拡張子のみに基づく事実であり、内容の推測は行わない。
    未対応の拡張子は Language.UNKNOWN とし、解析対象から除外する。
    """

    return _EXTENSION_TO_LANGUAGE.get(path.suffix.lower(), Language.UNKNOWN)
