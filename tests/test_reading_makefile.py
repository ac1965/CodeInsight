from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codeinsight.cli import main

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

pytestmark = pytest.mark.skipif(shutil.which("make") is None or shutil.which("uv") is None, reason="make / uv が必要")


def _make(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    # make reading は、既定でビューアーを起動して待ち続けるため、テストでは起動しない（SERVE=0）
    extra = [] if any(a.startswith("SERVE=") or a == "-n" for a in args) else ["SERVE=0"]
    return subprocess.run(["make", "--no-print-directory", *args, *extra], cwd=cwd, capture_output=True, text=True, timeout=300)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def test_make_reading_produces_the_reading_materials_without_touching_the_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURES / "layered", target)
    before = _snapshot(target)
    out = tmp_path / "out"

    result = _make("reading", f"TARGET={target}", f"OUT={out}", "TOP=3")

    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("README.md", "overview.txt", "architecture.txt", "boundaries.txt", "externals.txt", "analysis/unresolved.txt", "graphs/call.html", "graphs/arch.mmd"):
        logs = "\n".join(f"--- {f.name}\n{f.read_text(errors='replace')}" for f in sorted((out / "logs").glob("*"))) if (out / "logs").is_dir() else ""
        assert (out / name).is_file(), f"{name}\n{result.stdout}\n{result.stderr}\n{logs}"
    assert list((out / "functions").glob("*.txt")), "主要な関数の読解カードがある"
    index = (out / "README.md").read_text(encoding="utf-8")
    assert "この資料が対応している範囲（言語別）" in index and "実行順序や実際に通る経路を示すものではありません" in index
    assert "生成していません" in index  # AIは既定で使わない（AI_SEND=1 が無い）
    assert not (out / "ai").exists()
    assert _snapshot(target) == before  # 対象のソースは変更されない（出力は対象の外）


def test_make_reading_refuses_an_output_inside_the_target_and_a_missing_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURES / "layered", target)

    inside = _make("reading", f"TARGET={target}", f"OUT={target}/out")
    assert inside.returncode != 0 and "TARGET の外" in inside.stdout + inside.stderr
    assert not (target / "out" / "README.md").exists()

    assert _make("reading").returncode != 0  # TARGET 必須
    assert _make("reading", f"TARGET={tmp_path / 'nothing'}", f"OUT={tmp_path / 'o'}").returncode != 0


def test_make_c_build_requires_explicit_permission(tmp_path: Path) -> None:
    result = _make("reading-c-build", f"TARGET={FIXTURES / 'c_callgraph'}", f"OUT={tmp_path / 'o'}")
    assert result.returncode != 0 and "ALLOW_BUILD=1" in result.stdout + result.stderr  # 対象のconfigure/makeは許可なしに実行しない


def test_risks_now_checks_c_functions_and_does_not_claim_they_are_unchecked(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURES / "c_flow"), "--db", db]) == 0
    capsys.readouterr()
    assert main(["risks", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "unsafe-libc" in out and "command-exec" in out and "Pythonのみ対応" not in out  # Cは検査対象


@pytest.mark.parametrize("command, fragment", [
    (["config"], "設定値の検出はPythonのみ対応"),
    (["environment"], "実行環境の前提の抽出はPythonのみ対応"),
    (["boundaries"], "入口と境界の検出は、c ではmain 関数のみ対応"),
])
def test_language_limited_commands_say_what_they_did_not_check(tmp_path: Path, capsys, command: list[str], fragment: str) -> None:
    db = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURES / "c_callgraph"), "--db", db]) == 0
    capsys.readouterr()
    assert main([*command, "--db", db]) == 0
    assert fragment in capsys.readouterr().out  # 「0件」「確認できませんでした」を「問題なし」と読ませない（Pythonのみ対応の機能）


def test_coverage_note_goes_to_stderr_for_json_and_is_absent_for_python_only_projects(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURES / "c_callgraph"), "--db", db]) == 0
    capsys.readouterr()
    assert main(["config", "--format", "json", "--db", db]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "[]" and "Pythonのみ対応" in captured.err  # JSONの形式は変えない

    py = str(tmp_path / "p.sqlite")
    assert main(["analyze", str(FIXTURES / "python_flow"), "--db", py]) == 0
    capsys.readouterr()
    assert main(["risks", "--db", py]) == 0
    assert "Pythonのみ対応" not in capsys.readouterr().out


def test_understand_marks_unsupported_sections_instead_of_leaving_them_blank(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURES / "c_callgraph"), "--db", db]) == 0
    capsys.readouterr()
    assert main(["understand", "Calculator", "--db", db]) == 0  # 構造体は、関数内の解析の対象外
    out = capsys.readouterr().out
    assert out.count("対象外") >= 3 and "空欄は「なし」を意味しません" in out
    assert "ありません（静的に追える範囲）" not in out  # 対象外のものを「変更なし」と断定しない


def test_tilde_in_paths_is_expanded_even_when_the_shell_does_not_expand_it(tmp_path: Path) -> None:
    # OUT=~/x のように、シェルが展開しない渡し方（make の変数に ~ がそのまま入る）でも、同じ場所に出力する
    import os

    target = tmp_path / "target"
    shutil.copytree(FIXTURES / "layered", target)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        ["make", "--no-print-directory", "reading", f"TARGET={target}", "OUT=~/reading_out", "TOP=2", "SERVE=0"],
        cwd=ROOT, capture_output=True, text=True, timeout=300, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "reading_out" / "README.md").is_file() and (home / "reading_out" / "overview.txt").is_file()
    assert not (ROOT / "~").exists()  # リテラルの「~」ディレクトリを作らない
    assert "No such file" not in result.stdout + result.stderr


def test_paths_with_spaces_are_refused_clearly(tmp_path: Path) -> None:
    result = _make("reading", f"TARGET={FIXTURES / 'layered'}", f"OUT={tmp_path / 'a b'}")
    assert result.returncode == 2 and "空白は使えません" in result.stdout + result.stderr


def test_make_reading_shares_the_analyze_db_and_serves_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURES / "layered", target)
    out = tmp_path / "out"
    data = Path(os.environ["CODEINSIGHT_DATA_DIR"])  # conftest の autouse が、テストごとに隔離したDBの場所
    # 解析結果が無ければ、ビューアーを起動せずに案内して終了する
    missing = _make("reading-serve-run", f"TARGET={target}", f"OUT={out}")
    assert missing.returncode == 2 and "make analyze" in missing.stdout + missing.stderr
    # make analyze と同じDBに解析すると、reading は、同じDB・同じプロジェクトを使う（OUT 内に別のDBを作らない）
    assert main(["analyze", str(target), "--db", str(data / "codeinsight.db")]) == 0
    result = _make("reading", f"TARGET={target}", f"OUT={out}", "TOP=2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (out / "analysis" / "codeinsight.db").exists()
    assert (out / "overview.txt").read_text(encoding="utf-8").strip()
    # 起動コマンドの内容だけを確認する（-n: 実行しない）
    dry = _make("-n", "reading-serve-run", f"TARGET={target}", f"OUT={out}", "PORT=9123", "OPEN=1")
    assert "serve" in dry.stdout and "--port 9123" in dry.stdout and "--open" in dry.stdout and f"--project {target}" in dry.stdout
    # 既定（SERVE の指定なし）では、ビューアーを起動する。SERVE=0 なら、資料を作って終わる
    monkeypatch.delenv("SERVE")  # conftest が、他のテストで起動しないよう設定しているもの
    default = _make("-n", "reading", f"TARGET={target}", f"OUT={out}")
    assert 'if [ "1" != "0" ]' in default.stdout and "reading-serve-run" in default.stdout
    off = _make("-n", "reading", f"TARGET={target}", f"OUT={out}", "SERVE=0")
    assert 'if [ "0" != "0" ]' in off.stdout


def test_make_analyze_and_make_reading_run_the_same_analysis(tmp_path: Path) -> None:
    """`make reading` は最初に、`make analyze` と同じ解析（同じDB・同じ COMPILE_DB）を行う。事前の `make analyze` は不要。"""

    target, out, db = tmp_path / "t", tmp_path / "out", tmp_path / "x.db"
    shutil.copytree(FIXTURES / "layered", target)
    args = (f"TARGET={target}", f"OUT={out}", f"DB={db}", "COMPILE_DB=/tmp/cdb")
    analyze = _make("-n", "analyze", *args).stdout
    reading = _make("-n", "reading-analyze", *args).stdout
    wanted = f"analyze {target} --db {db} --compile-commands /tmp/cdb"
    assert wanted in analyze and wanted in reading
    # 事前の make analyze なしでも、reading だけで解析結果ができ、DB を共有する
    result = _make("reading", f"TARGET={target}", f"OUT={out}", f"DB={db}", "TOP=2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert db.is_file() and (out / "overview.txt").read_text(encoding="utf-8").strip()


def test_docker_targets_share_data_mount_the_target_read_only_and_publish_only_to_loopback(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.mkdir()
    real = str(target.resolve())
    data = tmp_path / "data"
    analyze = _make("-n", "docker-analyze", f"TARGET={target}", f"DOCKER_DATA={data}").stdout
    assert f'-v "{data}":/data' in analyze and f'-v "{real}":"{real}":ro' in analyze  # DBはホストと共有、対象は同じパスに読み取り専用
    assert '--user "$(id -u):$(id -g)"' in analyze and f'analyze "{real}"' in analyze
    serve = _make("-n", "docker-serve", f"TARGET={target}", "PORT=9123").stdout
    assert "-p 127.0.0.1:9123:9123" in serve and "--host 0.0.0.0" in serve  # ホスト側は 127.0.0.1 にだけ公開
    reading = _make("-n", "docker-reading", f"TARGET={target}", f"OUT={tmp_path / 'out'}", f"DOCKER_DATA={data}").stdout
    assert "SERVE=0" in reading and "UV=ci-uv" in reading and "AI_SEND" not in reading  # 既定ではAIへ送信しない
    assert _make("docker-analyze").returncode != 0  # TARGET 必須
    assert "AI_SEND=1" in _make("-n", "docker-reading", f"TARGET={target}", f"OUT={tmp_path / 'o'}", "AI_SEND=1").stdout


def test_compose_file_publishes_loopback_only_mounts_read_only_and_never_creates_host_paths() -> None:
    text = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    assert '"127.0.0.1:${PORT:-8765}:${PORT:-8765}"' in text and "0.0.0.0:" not in text  # ホスト側は 127.0.0.1 のみ
    assert "read_only: true" in text and text.count("create_host_path: false") == 3  # 対象は読み取り専用。存在しない場所を root の所有で作らせない
    assert "${TARGET:?" in text  # TARGET は必須
    assert "CODEINSIGHT_AI_ALLOW_SEND" in text and "AI_SEND:-" in text  # 送信の許可は、指定したときだけ
    assert "image: ollama" not in text  # Ollama のコンテナは含めない（ホストのものを使う）
