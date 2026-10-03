"""観測の収集器（言語別）の一覧。すべて未実装で、計画の表示にだけ使う。"""

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
    CollectorSpec("python-monitoring", "python", (K.CALL, K.COVERAGE, K.FAILURE, K.TIMING), "sys.monitoring（3.12+）/ sys.settrace"),
    CollectorSpec("python-audit", "python", (K.IO,), "sys.addaudithook（ファイル・ネットワーク・プロセスの監査イベント）"),
    CollectorSpec("c-gcov", "c", (K.COVERAGE, K.CALL), "gcov（--coverage で再コンパイル）", needs_rebuild=True),
    CollectorSpec("c-instrument", "c", (K.CALL, K.TIMING), "-finstrument-functions（再コンパイル）", needs_rebuild=True),
    CollectorSpec("c-syscall", "c", (K.IO, K.FAILURE), "strace / dtrace / ltrace（OSによる）"),
)


def collectors_for(languages: set[str]) -> list[CollectorSpec]:
    return [c for c in COLLECTORS if c.language in languages]
