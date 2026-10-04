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


def test_helper_binary_is_separate_per_platform_so_host_and_container_can_share_the_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_go = tmp_path / "fakego"
    fake_go.write_text('#!/bin/sh\ntouch "$3"\n', encoding="utf-8")  # go build -o <出力> . の出力だけ作る
    fake_go.chmod(0o755)
    monkeypatch.setenv("CODEINSIGHT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(go_analyzer.shutil, "which", lambda name: str(fake_go))
    monkeypatch.setattr(go_analyzer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(go_analyzer.platform, "machine", lambda: "arm64")
    mac = _Helper()._build()
    monkeypatch.setattr(go_analyzer.platform, "system", lambda: "Linux")
    linux = _Helper()._build()
    assert mac != linux and mac.name.endswith("-darwin-arm64") and linux.name.endswith("-linux-arm64")
    assert mac.parent == linux.parent == tmp_path / "data" / "tools"  # 同じ場所を共有しても、互いの実行ファイルを取り違えない
