from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codeinsight.analysis import CAnalyzer, PythonAnalyzer, SymbolExtractor
from codeinsight.application import AnalysisCoordinator, ProjectManager
from codeinsight.domain import AnalysisStatus, Language, Project
from codeinsight.infrastructure import AnalysisRepository, default_db_path


def _build_symbol_extractor(compile_commands_dir: Path | None) -> SymbolExtractor:
    return SymbolExtractor(
        {
            Language.C: CAnalyzer(compile_commands_dir=compile_commands_dir),
            Language.PYTHON: PythonAnalyzer(),
        }
    )


def _get_or_create_project(project_manager: ProjectManager, root_path: Path) -> Project:
    root_path = root_path.resolve()
    for project in project_manager.list():
        if project.root_path == root_path:
            return project
    return project_manager.register(root_path)


def _cmd_analyze(args: argparse.Namespace) -> int:
    root_path = Path(args.path)
    if not root_path.is_dir():
        print(f"エラー: ディレクトリが存在しません: {root_path}", file=sys.stderr)
        return 1

    db_path = Path(args.db) if args.db else default_db_path()
    compile_commands_dir = Path(args.compile_commands) if args.compile_commands else root_path

    with AnalysisRepository(db_path) as repository:
        project_manager = ProjectManager(repository)
        project = _get_or_create_project(project_manager, root_path)

        symbol_extractor = _build_symbol_extractor(
            compile_commands_dir if (compile_commands_dir / "compile_commands.json").is_file() else None
        )
        coordinator = AnalysisCoordinator(repository, symbol_extractor)
        result = coordinator.analyze_project(project)

        source_files = repository.list_source_files(project.project_id)
        symbols = repository.list_symbols_for_project(project.project_id)

    print(f"プロジェクト: {project.name} ({project.project_id})")
    print(f"データベース: {db_path}")
    print(f"解析対象ファイル数: {len(source_files)}")
    print(f"抽出シンボル数: {len(symbols)}")
    print(f"解析結果: {result.status.value}")
    if result.warnings:
        print(f"警告 {len(result.warnings)}件:")
        for warning in result.warnings:
            print(f"  - {warning}")
    if result.errors:
        print(f"エラー {len(result.errors)}件:")
        for error in result.errors:
            print(f"  - {error}")

    return 0 if result.status != AnalysisStatus.FAILED else 1


def _cmd_symbols(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"エラー: データベースが見つかりません: {db_path}", file=sys.stderr)
        return 1

    with AnalysisRepository(db_path) as repository:
        project = repository.get_project(args.project_id) if args.project_id else None
        if args.project_id and project is None:
            print(f"エラー: プロジェクトが見つかりません: {args.project_id}", file=sys.stderr)
            return 1
        if project is None:
            projects = repository.list_projects()
            if not projects:
                print("プロジェクトが登録されていません。")
                return 0
            project = projects[0]

        source_files = {f.file_id: f for f in repository.list_source_files(project.project_id)}
        symbols = repository.list_symbols_for_project(project.project_id)

    if args.file:
        symbols = [s for s in symbols if source_files[s.file_id].relative_path == args.file]

    print(f"プロジェクト: {project.name} ({project.project_id})")
    for symbol in sorted(symbols, key=lambda s: (source_files[s.file_id].relative_path, s.start_line)):
        relative_path = source_files[symbol.file_id].relative_path
        print(
            f"{relative_path}:{symbol.start_line}-{symbol.end_line}\t"
            f"{symbol.kind.value}\t{symbol.qualified_name}"
        )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeinsight",
        description="C言語・Pythonを中心としたコードリーディング支援ソフトウェア（Phase1: 解析基盤）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="プロジェクトを走査・解析し、結果を保存する")
    analyze_parser.add_argument("path", help="解析対象ディレクトリ")
    analyze_parser.add_argument("--db", help="解析結果DBのパス（既定: ~/.codeinsight/codeinsight.db）")
    analyze_parser.add_argument(
        "--compile-commands", help="compile_commands.jsonを含むディレクトリ（既定: 解析対象ルート）"
    )
    analyze_parser.set_defaults(func=_cmd_analyze)

    symbols_parser = subparsers.add_parser("symbols", help="保存済みのシンボルを一覧表示する")
    symbols_parser.add_argument("--db", required=True, help="解析結果DBのパス")
    symbols_parser.add_argument("--project-id", help="プロジェクトID（省略時は先頭のプロジェクト）")
    symbols_parser.add_argument("--file", help="相対パスで絞り込む")
    symbols_parser.set_defaults(func=_cmd_symbols)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
