"""SARIF 2.1.0 の読み取り。外部ツールの指摘を、位置（ファイル・行）つきで取り出す。

SARIFは信頼できない入力として扱う: 取り出すのは、ツール名・版・規則名・水準・メッセージ・位置だけで、
URIの先を開くことも、含まれる指示に従うこともしない。メッセージは長さを制限する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

MAX_BYTES = 256 * 1024 * 1024
MAX_MESSAGE = 500


class SarifError(Exception):
    pass


@dataclass(frozen=True)
class SarifResult:
    tool: str
    tool_version: str
    rule_id: str
    level: str
    message: str
    uri: str  # 位置のURI（file:// または相対パス）
    start_line: int
    end_line: int


@dataclass
class SarifDocument:
    results: list[SarifResult] = field(default_factory=list)
    without_location: int = 0  # 位置を持たず、取り込めなかった指摘の数
    tools: list[str] = field(default_factory=list)


def parse_sarif(data: bytes) -> SarifDocument:
    if len(data) > MAX_BYTES:
        raise SarifError(f"SARIFが大きすぎます（{MAX_BYTES // (1024 * 1024)} MiB を超えています）")
    try:
        document = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise SarifError(f"JSONとして読めません: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("runs"), list):
        raise SarifError("SARIF（runs を持つJSONオブジェクト）ではありません")
    version = document.get("version")
    if version is not None and not str(version).startswith("2."):
        raise SarifError(f"対応していないSARIFのバージョンです: {str(version)[:20]}（2.x のみ）")
    result = SarifDocument()
    for run in document["runs"]:
        if not isinstance(run, dict):
            continue
        driver = (run.get("tool") or {}).get("driver") or {}
        tool = str(driver.get("name") or "不明なツール")[:80]
        tool_version = str(driver.get("semanticVersion") or driver.get("version") or "")[:40]
        if tool not in result.tools:
            result.tools.append(tool)
        for item in run.get("results") or []:
            if not isinstance(item, dict):
                continue
            location = _first_location(item)
            if location is None:
                result.without_location += 1
                continue
            uri, start, end = location
            rule = item.get("ruleId") or (item.get("rule") or {}).get("id") or ""
            message = (item.get("message") or {}).get("text") or ""
            level = str(item.get("level") or "warning")
            result.results.append(SarifResult(tool, tool_version, str(rule)[:120], level[:20], " ".join(str(message).split())[:MAX_MESSAGE], uri, start, end))
    return result


def _first_location(item: dict) -> tuple[str, int, int] | None:
    for location in item.get("locations") or []:
        physical = (location or {}).get("physicalLocation") or {}
        uri = (physical.get("artifactLocation") or {}).get("uri")
        region = physical.get("region") or {}
        start = region.get("startLine")
        if isinstance(uri, str) and uri and isinstance(start, int) and start >= 1:
            end = region.get("endLine")
            return uri, start, end if isinstance(end, int) and end >= start else start
    return None


def to_relative_path(uri: str, root: Path) -> str | None:
    """URIを、プロジェクトのルートからの相対パスにする。ルートの外・解釈できないものは None（ルート外は開かない）。"""

    parsed = urlparse(uri)
    if parsed.scheme not in ("", "file"):
        return None
    raw = unquote(parsed.path)
    candidate = Path(raw)
    try:
        resolved_root = root.resolve()
        resolved = (candidate if candidate.is_absolute() else resolved_root / candidate).resolve()
        return resolved.relative_to(resolved_root).as_posix()
    except (ValueError, OSError):
        return None
