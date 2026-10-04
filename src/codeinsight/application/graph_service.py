"""グラフ（呼び出し・ファイル依存・継承・アーキテクチャ・制御フロー）の組み立て。CLIとWebサーバーで共有する。

引数の解釈（シンボル名の解決・エラー表示）は呼び出し側が行い、ここには解決済みのシンボルを渡す。
"""

from __future__ import annotations

from dataclasses import dataclass

from codeinsight.application.architecture_service import ArchitectureService
from codeinsight.application.c_flow_service import CFlowService
from codeinsight.application.cfg_builder import CfgBuilder
from codeinsight.application.cfg_builder_c import CCfgBuilder
from codeinsight.application.cfg_builder_lisp import LispCfgBuilder
from codeinsight.application.elisp_flow_service import ElispFlowService
from codeinsight.application.external_service import ExternalService
from codeinsight.application.flow_service import FlowAnalysisError, FlowService
from codeinsight.application.graph_builder import GraphBuilder, GraphModel, Traversal
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Language, Project, Symbol

GRAPH_KINDS = ("call", "deps", "inherit", "flow", "arch")


class GraphRequestError(Exception):
    """グラフの指定が不正（必要な起点がない・ファイルが見つからないなど）。"""


@dataclass(frozen=True)
class GraphRequest:
    kind: str  # GRAPH_KINDS のいずれか
    root_symbol: Symbol | None = None  # call / inherit / flow の起点（解決済み）
    root_path: str | None = None  # deps の起点（プロジェクトのルートからの相対パス）
    depth: int | None = None
    direction: Traversal = Traversal.BOTH
    include_external: bool = False
    include_unresolved: bool = True


class GraphService:
    def __init__(self, navigation: NavigationService) -> None:
        self._navigation = navigation

    def build(self, project: Project, index: ProjectIndex, request: GraphRequest) -> GraphModel:
        builder = GraphBuilder(index)
        if request.kind == "call":
            return builder.call_graph(
                request.root_symbol, request.depth, request.direction,
                include_unresolved=request.include_unresolved, include_external=request.include_external,
            )
        if request.kind == "inherit":
            return builder.inheritance_graph(request.root_symbol, request.depth, request.direction)
        if request.kind == "arch":
            return builder.architecture_graph(ArchitectureService().build(index, ExternalService().report(index), request.depth))
        if request.kind == "flow":
            return self._flow(project, index, request.root_symbol)
        if request.kind == "deps":
            try:
                return builder.file_dependency_graph(
                    request.root_path, request.depth, request.direction,
                    include_external=request.include_external, include_unresolved=request.include_unresolved,
                )
            except LookupError as exc:
                raise GraphRequestError(f"ファイルが見つかりません: {request.root_path}") from exc
        raise GraphRequestError(f"未対応のグラフの種類です: {request.kind}")

    def _flow(self, project: Project, index: ProjectIndex, symbol: Symbol | None) -> GraphModel:
        if symbol is None:
            raise GraphRequestError("制御フロー図には、起点の関数・メソッドの指定が必要です。")
        language = index.files[symbol.file_id].language
        path = index.path_of(symbol.file_id)
        try:
            if language == Language.C:
                return CCfgBuilder().build(CFlowService(self._navigation).load(project, index, symbol), path, symbol.qualified_name)
            if language == Language.ELISP:
                return LispCfgBuilder().build(ElispFlowService(self._navigation).load(project, index, symbol), path, symbol.qualified_name)
            function = FlowService(self._navigation).function_ast(project, index, symbol)
        except (FlowAnalysisError, ValueError) as exc:
            raise GraphRequestError(str(exc)) from exc
        return CfgBuilder().build(function, path, symbol.qualified_name)

    @staticmethod
    def annotate(model: GraphModel, project: Project, stale: list[str]) -> GraphModel:
        """プロジェクト名・リビジョン・解析後に変更されたファイルを、グラフに付す（古い内容を最新の事実として示さない）。"""

        model.meta.update({"project": project.name, "repository_revision": project.repository_revision, "stale_files": stale})
        if stale:
            model.notes.append(f"解析後に変更されたファイルがあります（{len(stale)}件）。内容が古い可能性があります。")
        return model
