"""C23 構文の解析: 既定の C11 で失敗したときの C23 再試行と、新しい libclang の選択（CODEINSIGHT_LIBCLANG）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codeinsight.analysis import libclang_select
from codeinsight.analysis.c_analyzer import CAnalyzer
from codeinsight.analysis.fingerprint import analyzer_fingerprint
from codeinsight.analysis.language_adapter import SourceUnit

C23_SOURCE = "bool flag = true;\nint twice(int v) { return v * 2; }\n"  # bool/true は C23 でキーワード（C11 では未宣言）


def analyze(tmp_path: Path, text: str, name: str = "a.c"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return CAnalyzer().analyze_file(SourceUnit(file_id="f1", absolute_path=path, relative_path=name, content=text))


def test_default_c11_failure_is_retried_as_c23_and_recorded_as_inferred(tmp_path: Path) -> None:
    result = analyze(tmp_path, C23_SOURCE)
    assert result.errors == []  # 解析できる
    assert any("C23 として解析しました（推定" in w for w in result.warnings)  # 確定ではなく推定として記録する


def test_genuine_errors_are_not_hidden_by_the_retry(tmp_path: Path) -> None:
    result = analyze(tmp_path, "int broken( {\n")
    assert result.errors  # C23 でも失敗するものは、元のエラーを残す（正常として扱わない）
    assert not any("C23 として解析しました" in w for w in result.warnings)


def test_plain_c11_is_not_reported_as_c23(tmp_path: Path) -> None:
    result = analyze(tmp_path, "int one(void) { return 1; }\n")
    assert result.errors == [] and not any("C23" in w for w in result.warnings)


def test_invalid_libclang_setting_falls_back_with_a_warning(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(libclang_select.ENV, str(tmp_path / "missing.dylib"))
    monkeypatch.setattr(libclang_select, "_state", {})
    with pytest.warns(UserWarning, match="同梱の libclang"):
        assert "同梱" in libclang_select.configure()


def test_fingerprint_changes_with_the_libclang_setting(monkeypatch) -> None:
    monkeypatch.delenv(libclang_select.ENV, raising=False)
    before = analyzer_fingerprint()
    monkeypatch.setenv(libclang_select.ENV, "/somewhere/libclang.so")
    assert analyzer_fingerprint() != before  # libclang を替えたら、変更のないファイルも再解析される


def _recent_libclang() -> list[str]:
    """`_Countof` を解析できる版（clang 21 以降）と分かる候補だけ。パスに LLVM の版がある場合はそれで、macOS の Xcode / Command Line Tools は同梱の版が新しいものとして扱う。"""

    import re

    found = []
    for path in libclang_select.candidates():
        match = re.search(r"llvm[-@]?(\d+)", path)
        if (match and int(match.group(1)) >= 21) or "Xcode" in path or "CommandLineTools" in path:
            found.append(path)
    return found


@pytest.mark.skipif(not _recent_libclang(), reason="clang 21 以降の libclang が見つからない（CIの Ubuntu は LLVM 18）")
def test_newer_libclang_parses_syntax_the_bundled_one_cannot(tmp_path: Path) -> None:
    (tmp_path / "x.c").write_text("#include <stddef.h>\nint arr[4];\nsize_t n(void) { return _Countof(arr); }\n", encoding="utf-8")
    code = (
        "import json,sys\nfrom pathlib import Path\n"
        "from codeinsight.analysis.c_analyzer import CAnalyzer\nfrom codeinsight.analysis.language_adapter import SourceUnit\n"
        "p=Path(sys.argv[1])/'x.c'; t=p.read_text()\n"
        "r=CAnalyzer().analyze_file(SourceUnit(file_id='f',absolute_path=p,relative_path='x.c',content=t))\n"
        "print(json.dumps({'errors':r.errors}))\n"
    )
    env = {**os.environ, libclang_select.ENV: _recent_libclang()[0]}
    done = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.splitlines()[-1])["errors"] == []


def test_unresolvable_include_does_not_crash_the_analysis(tmp_path: Path, monkeypatch) -> None:
    """インクルードされるファイルが取れない（バインディングが AssertionError を出す）場合も、解析全体を落とさず、解決できない依存として残す。

    emacs では、取り込む側の文脈で解析したヘッダーなどで、これが原因のファイル失敗が10件あった。
    """

    import clang.cindex as cindex

    def raising(self):
        raise AssertionError

    monkeypatch.setattr(cindex.Cursor, "get_included_file", raising)
    result = analyze(tmp_path, "#include <stddef.h>\nint kept(void) { return 1; }\n")
    assert not any("AssertionError" in e for e in result.errors)
    assert any(d.target_name == "stddef.h" and d.target_key is None and "特定できない" in d.note for d in result.dependencies)
    assert any(sym.name == "kept" for sym in result.symbols)


def test_trailing_separator_in_compile_command_is_dropped(tmp_path: Path) -> None:
    """compile_commands.json の引数の末尾の `--`（補間されたヘッダーのコマンドに付く）を残すと、libclang が解析に失敗する。"""

    import clang.cindex as cindex

    db_dir = tmp_path / "build"
    db_dir.mkdir()
    header = tmp_path / "h.h"
    header.write_text("typedef int number;\n", encoding="utf-8")
    source = tmp_path / "s.c"
    source.write_text('#include "h.h"\nnumber n;\n', encoding="utf-8")
    (db_dir / "compile_commands.json").write_text(
        json.dumps([{"directory": str(tmp_path), "file": str(source), "arguments": ["cc", "-c", "-x", "c-header", "--", str(source)]}]), encoding="utf-8"
    )
    analyzer = CAnalyzer(compile_commands_dir=db_dir)
    command = analyzer._compilation_database.getCompileCommands(str(source))[0]  # type: ignore[union-attr]
    assert isinstance(command, cindex.CompileCommand) and "--" not in analyzer._command_args(command)
