from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Project, Symbol
from codeinsight.infrastructure.git_repository import Commit, GitRepository


@dataclass
class SymbolHistory:
    symbol: Symbol
    path: str
    commits: list[Commit] = field(default_factory=list)  # 新しい順
    available: bool = True  # Gitの履歴を取得できたか
    modified_in_working_tree: bool = False  # 未コミットの変更があり、行範囲の対応がずれている可能性

    @property
    def introduced(self) -> Commit | None:
        return self.commits[-1] if self.commits else None


@dataclass
class FileChurn:
    path: str
    commits: int
    authors: int
    first_date: str
    last_date: str
    lines: int

    @property
    def hotspot_score(self) -> int:
        return self.commits * max(self.lines, 1)


@dataclass
class HistoryReport:
    churn: list[FileChurn]
    coupling: list[tuple[str, str, int]]  # (ファイルA, ファイルB, 同時に変更されたコミット数)
    commits_examined: int
    available: bool


class HistoryService:
    """Gitの履歴（読み取り専用）から、変更の経緯・変更頻度・同時に変更されるファイルを調べる。

    履歴は「なぜ今の実装になったか」の手がかり（コミットメッセージ・日付・作者）であって、理由そのものは
    コードからは確認できない。リネーム・大規模な整形・コミットの粒度によって、履歴は実態を反映しない場合がある。
    """

    def __init__(self, git: GitRepository | None = None) -> None:
        self._git = git or GitRepository()

    def symbol_history(self, project: Project, index: ProjectIndex, symbol: Symbol, limit: int = 8) -> SymbolHistory:
        path = index.path_of(symbol.file_id)
        history = SymbolHistory(symbol, path)
        if not self._git.is_git_repository(project.root_path):
            history.available = False
            return history
        history.modified_in_working_tree = self._git.file_is_modified(project.root_path, path)
        commits = self._git.commits_for_lines(project.root_path, path, symbol.start_line, symbol.end_line, limit)
        if commits is None:
            history.available = False
        else:
            history.commits = commits
        return history

    def report(self, project: Project, index: ProjectIndex, max_commits: int = 2000, min_coupling: int = 3) -> HistoryReport:
        commits = self._git.file_history(project.root_path, max_commits)
        if commits is None:
            return HistoryReport([], [], 0, False)
        known = {f.relative_path: f for f in index.files.values()}
        lines: dict[str, int] = defaultdict(int)
        for symbol in index.symbols.values():
            path = index.path_of(symbol.file_id)
            lines[path] = max(lines[path], symbol.end_line)
        per_file: dict[str, list[Commit]] = defaultdict(list)
        pair_counts: Counter[tuple[str, str]] = Counter()
        for commit in commits:
            files = [f for f in commit.files if f in known]
            for path in files:
                per_file[path].append(commit)
            if 1 < len(files) <= 30:  # 一括変更（巨大なコミット）は同時変更の根拠として弱いため除く
                pair_counts.update(combinations(sorted(files), 2))
        churn = [
            FileChurn(path, len(cs), len({c.author for c in cs}), cs[-1].date, cs[0].date, lines[path])
            for path, cs in per_file.items()
        ]
        churn.sort(key=lambda c: (-c.hotspot_score, c.path))
        coupling = [(a, b, n) for (a, b), n in pair_counts.most_common() if n >= min_coupling]
        return HistoryReport(churn, coupling, len(commits), True)
