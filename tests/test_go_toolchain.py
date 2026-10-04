from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.analysis import go_analyzer
from codeinsight.analysis.go_analyzer import GoAnalyzer, GoToolchainError, _Helper
from codeinsight.analysis.language_adapter import SourceUnit


def test_missing_go_toolchain_is_reported_as_a_failed_file_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "a.go"
    path.write_text("package a\n", encoding="utf-8")
    monkeypatch.setattr(go_analyzer.shutil, "which", lambda name: None)
    monkeypatch.setattr(go_analyzer, "_HELPER", _Helper())  # 補助プログラムの状態を、このテストだけのものにする
    result = GoAnalyzer().analyze_file(SourceUnit.from_path("f", path))
    assert not result.succeeded and "Goのツールチェーン" in result.errors[0]  # 解析できなかったファイルを、正常として扱わない
    with pytest.raises(GoToolchainError):
        go_analyzer._HELPER.request("a.go", b"package a\n")  # 以降の要求も、同じ理由で失敗する（毎回ビルドを試みない）
