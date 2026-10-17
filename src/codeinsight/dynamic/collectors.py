"""観測の収集器（言語別）の一覧。実装済みは `implemented=True`（それ以外は計画の表示にだけ使う）。"""

from __future__ import annotations

from dataclasses import dataclass

from codeinsight.domain.observation import ObservationKind

K = ObservationKind


@dataclass(frozen=True)
class CollectorSpec:
    name: str
    language: str
    kinds: tuple[ObservationKind, ...]
    mechanism: str
    needs_rebuild: bool = False  # 再コンパイルを伴う（ビルドの許可とは別に、実行の許可も必要）
    implemented: bool = False


COLLECTORS: tuple[CollectorSpec, ...] = (
    CollectorSpec("python-monitoring", "python", (K.CALL, K.COVERAGE, K.FAILURE), "sys.monitoring（3.12+）/ sys.settrace", implemented=True),
    CollectorSpec("python-audit", "python", (K.IO,), "sys.addaudithook（ファイル・ネットワーク・プロセスの監査イベント）"),
    CollectorSpec("c-gcov", "c", (K.COVERAGE,), "gcov（--coverage で、作業領域の複製を再コンパイル。関数・行の実行回数のみ。呼び出しの辺は得られない）", needs_rebuild=True, implemented=True),
    CollectorSpec("c-instrument", "c", (K.CALL, K.IO), "-finstrument-functions と薄い実行時の層（呼び出しの辺と、入出力の種類。addr2line で関数名にする。パス・コマンド・値は記録しない）", needs_rebuild=True, implemented=True),
    CollectorSpec("c-syscall", "c", (K.IO, K.FAILURE), "strace / dtrace / ltrace（OSによる）"),
)


def collectors_for(languages: set[str]) -> list[CollectorSpec]:
    return [c for c in COLLECTORS if c.language in languages]
