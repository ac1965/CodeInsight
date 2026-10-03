"""`make reading` の成果物（OUT ディレクトリ）を、1つの自己完結の印刷用HTMLにまとめる（PDFの元になる）。

* 対象のソース由来の文字列を含むため、すべてエスケープする。スクリプトは含めず、Content-Security-Policy で
  スクリプトと外部通信を遮断する（ブラウザで開いても、PDFに変換しても、外へ通信しない）。
* 図は、graphviz（dot）があれば SVG にして埋め込む。無い場合や、ノードが多くて読めない場合は、省略したことを明示する。
* 長い出力は行数で打ち切り、打ち切ったことと、元のファイルを示す（黙って切らない）。
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_MAX_LINES = 400
DEFAULT_MAX_GRAPH_NODES = 60

# (章の見出し, [(ファイル, 説明)])。`make reading` の目次（README.md）と同じ順序
SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("全体像", (("overview.txt", "言語・主要モジュール・入口・中心となる関数"), ("architecture.txt", "コンポーネント構成・層構造・循環・外部連携"))),
    ("入口と境界", (
        ("boundaries.txt", "入口・CLI・HTTP・イベント・スレッド・非同期・キャッシュ"), ("config.txt", "環境変数・CLIオプション・設定ファイル・定数"),
        ("externals.txt", "外部ライブラリ・システムへの入出力"), ("environment.txt", "実行環境の前提"),
    )),
    ("注意して読む箇所", (
        ("risks.txt", "危険な書き方の手がかり"), ("tests-untested.txt", "テストから到達できない関数"), ("unused.txt", "使われていない可能性のあるコード"),
        ("analysis/unresolved.txt", "静的に確定できなかった関係（理由別）"),
    )),
    ("背景", (("history.txt", "変更履歴の手がかり"), ("docs-check.txt", "文書とコードのずれの候補"))),
)
GRAPHS = (("arch", "コンポーネント間の依存"), ("deps", "ファイル間の依存"), ("call", "関数呼び出し"))

CSS = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm; @bottom-center { content: counter(page) " / " counter(pages); font-size: 8pt; color: #666; } }
* { box-sizing: border-box; }
body { font-family: "Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans CJK JP","Noto Sans JP","Yu Gothic","Meiryo",sans-serif; font-size: 9.5pt; line-height: 1.6; color: #1a1a1a; margin: 0; }
h1 { font-size: 17pt; border-bottom: 2px solid #1f3a5f; padding-bottom: 4px; margin: 0 0 10px; page-break-before: always; color: #1f3a5f; }
h1.first { page-break-before: avoid; }
h2 { font-size: 12pt; margin: 18px 0 6px; color: #1f3a5f; page-break-after: avoid; }
h3 { font-size: 10.5pt; margin: 14px 0 4px; page-break-after: avoid; }
p, li { margin: 3px 0; }
ul { padding-left: 18px; margin: 4px 0; }
code { font-family: "SF Mono",Menlo,Consolas,"Noto Sans Mono CJK JP","Hiragino Sans",monospace; font-size: 8.6pt; background: #f1f3f5; padding: 0 3px; border-radius: 3px; }
pre { font-family: "SF Mono",Menlo,Consolas,"Noto Sans Mono CJK JP","Hiragino Sans",monospace; font-size: 7.6pt; line-height: 1.45; background: #f6f8fa; border: 1px solid #d8dee4; border-radius: 4px; padding: 6px 8px; white-space: pre-wrap; overflow-wrap: anywhere; margin: 4px 0 8px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 9pt; }
th, td { border: 1px solid #c9d1d9; padding: 3px 6px; text-align: left; vertical-align: top; }
th { background: #eef2f7; }
.cover { padding-top: 60mm; }
.cover h1 { font-size: 26pt; border: none; page-break-before: avoid; }
.meta td:first-child { width: 30%; background: #eef2f7; font-weight: 600; }
.note { border-left: 4px solid #d9a400; background: #fff8e1; padding: 5px 9px; margin: 8px 0; font-size: 9pt; }
.ai { border-left: 4px solid #8250df; background: #f5f0ff; padding: 5px 9px; margin: 8px 0; font-size: 9pt; }
.muted { color: #666; font-size: 8.5pt; }
.toc li { list-style: none; } .toc ul { padding-left: 0; } .toc ul ul { padding-left: 16px; }
a { color: #0b57d0; text-decoration: none; }
figure { margin: 8px 0; page-break-inside: avoid; } figure svg { max-width: 100%; height: auto; } figcaption { font-size: 8.5pt; color: #555; }
.cap { page-break-inside: avoid; }
"""


@dataclass
class Report:
    html: str
    sections: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)  # 省略・打ち切りしたもの（理由つき）
    graphs: list[str] = field(default_factory=list)  # 図として埋め込んだもの


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-").lower() or "section"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _inline(text: str, anchors: dict[str, str]) -> str:
    """README の範囲の記法（`code`・**太字**・[文字](リンク)）を、エスケープしたうえでHTMLにする。"""

    escaped = _e(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)

    def link(match: re.Match) -> str:
        label, target = match.group(1), html.unescape(match.group(2))
        anchor = anchors.get(target)
        return f'<a href="#{anchor}">{label}</a>' if anchor else label  # 資料に含めないファイルへのリンクは、文字だけ

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, escaped)


def _markdown_lines(lines: list[str], anchors: dict[str, str]) -> str:
    """見出し・箇条書き・コードブロック・段落だけの簡易な変換（README.md の範囲）。"""

    out: list[str] = []
    in_list = in_code = False
    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</pre>")
            else:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append("<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(_e(line))
            continue
        stripped = line.strip()
        if stripped.startswith(("* ", "- ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(stripped[2:], anchors)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if stripped:
            out.append(f"<p>{_inline(stripped, anchors)}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def _readme_section(readme: str, heading: str) -> list[str]:
    lines = readme.splitlines()
    for number, line in enumerate(lines):
        if line.startswith("## ") and line[3:].startswith(heading):
            body = []
            for following in lines[number + 1:]:
                if following.startswith("## "):
                    break
                body.append(following)
            return body
    return []


def _ai_summary(text: str) -> tuple[str, str, str]:
    """AI解説のファイルから、(モデル, 検証状態, 引用) を取り出す。見つからないものは「—」。"""

    model = re.search(r"モデル: ([^/\n]+?)\s*(?:/|$)", text)
    state = re.search(r"検証結果(?:（生成時）)?: ([^（\n]+)", text)
    cites = re.search(r"引用 (\d+)件のうち検証できたもの (\d+)件", text)
    return (
        model.group(1).strip() if model else "—", state.group(1).strip() if state else "—",
        f"{cites.group(2)} / {cites.group(1)}" if cites else "—",
    )


def _text_block(text: str, max_lines: int, source: str, omitted: list[str]) -> str:
    lines = text.rstrip("\n").splitlines()
    shown = lines[:max_lines]
    html_text = f"<pre>{_e(chr(10).join(shown)) or '（出力なし）'}</pre>"
    if len(lines) > max_lines:
        omitted.append(f"{source}: {len(lines)} 行のうち先頭 {max_lines} 行のみ")
        html_text += f'<p class="note">全 {len(lines)} 行のうち、先頭 {max_lines} 行だけを載せています。全体は <code>{_e(source)}</code> を参照してください。</p>'
    return html_text


def _render_graph(dot_text: str, max_nodes: int) -> tuple[str | None, str]:
    """DOT をSVGにする。戻り値: (SVG または None, 省略の理由)。"""

    nodes = sum(1 for line in dot_text.splitlines() if "[label=" in line and "->" not in line)
    if nodes > max_nodes:
        return None, f"ノードが {nodes} 個あり、1ページでは読めないため省略（対話的な graphs/*.html を参照）"
    if shutil.which("dot") is None:
        return None, "graphviz（dot）が見つからないため省略（graphs/*.html を参照）"
    try:
        completed = subprocess.run(["dot", "-Tsvg"], input=dot_text, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"図の描画に失敗したため省略（{exc.__class__.__name__}）"
    if completed.returncode != 0 or "<svg" not in completed.stdout:
        return None, "図の描画に失敗したため省略（graphviz がエラーを返した）"
    svg = completed.stdout[completed.stdout.index("<svg"):]
    return svg, ""


def build_report(out_dir: Path, *, target: str = "", max_lines: int = DEFAULT_MAX_LINES, max_graph_nodes: int = DEFAULT_MAX_GRAPH_NODES) -> Report:
    out_dir = Path(out_dir)
    report = Report("")
    readme = _read(out_dir / "README.md") or ""
    try:
        overview = json.loads(_read(out_dir / "overview.json") or "{}")
    except ValueError:
        overview = {}
    project = overview.get("project") or out_dir.name
    anchors: dict[str, str] = {}
    toc: list[tuple[str, str, list[tuple[str, str]]]] = []  # (id, 見出し, 小見出し)
    body: list[str] = []

    def chapter(title: str, content: str, subs: list[tuple[str, str]] | None = None) -> None:
        identifier = f"c{len(toc) + 1}-{_slug(title)}"
        toc.append((identifier, title, subs or []))
        report.sections.append(title)
        body.append(f'<section><h1 id="{identifier}">{len(toc)}. {_e(title)}</h1>\n{content}</section>')

    # --- 表紙・読み方 ---
    try:
        from importlib.metadata import version

        tool_version = version("codeinsight")
    except Exception:
        tool_version = "?"
    languages = ", ".join(f"{k} {v}ファイル" for k, v in (overview.get("languages") or {}).items()) or "-"
    status = overview.get("reference_status") or {}
    meta = [
        ("対象", target or overview.get("root") or project), ("リビジョン", overview.get("revision") or "-"), ("言語", languages),
        ("参照の解決状況", ", ".join(f"{k} {v}" for k, v in status.items()) or "-"),
        ("解析に失敗したファイル", f"{len(overview.get('failed_files', []))} 件"), ("解析後に変更されたファイル", f"{len(overview.get('stale_files', []))} 件"),
        ("生成", f"{datetime.now():%Y-%m-%d %H:%M}（CodeInsight {tool_version}）"),
    ]
    cover = (
        f'<div class="cover"><h1 class="first">コードリーディング資料</h1><h2>{_e(project)}</h2>'
        f'<table class="meta">{"".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in meta)}</table>'
        '<p class="note">この資料は、静的解析で確認できた事実をまとめたものです。実行順序や実際に通る経路を示すものではありません。'
        '「推定」「未解決」「曖昧」「外部」は確定ではありません。AI解説は、解析結果とは別に示します。</p></div>'
    )

    # 章の見出しに対応する、ファイル → アンカー
    for _title, items in SECTIONS:
        for name, _ in items:
            anchors[name] = f"f-{_slug(name)}"
    anchors["functions/"] = "functions"
    anchors["graphs/call.html"] = anchors["graphs/arch.html"] = anchors["graphs/deps.html"] = "graphs"

    reading = _markdown_lines(_readme_section(readme, "この資料が対応している範囲"), anchors) or "<p class=\"muted\">（目次が見つかりません）</p>"
    notes = _markdown_lines(_readme_section(readme, "この資料の読み方"), anchors)
    chapter("この資料の読み方", f"<h2>言語別の対応範囲</h2>{reading}<h2>事実と推論の区別</h2>{notes}")

    # --- 全体像（図つき） ---
    def file_block(name: str, caption: str) -> tuple[str, str]:
        text = _read(out_dir / name)
        identifier = anchors.get(name, f"f-{_slug(name)}")
        if text is None:
            report.omitted.append(f"{name}: 生成されていません")
            return identifier, f'<h2 id="{identifier}">{_e(name)}</h2><p class="muted">{_e(caption)}</p><p class="note">生成されていません。</p>'
        return identifier, f'<h2 id="{identifier}">{_e(name)}</h2><p class="muted">{_e(caption)}</p>{_text_block(text, max_lines, name, report.omitted)}'

    graph_html: list[str] = []
    for kind, caption in GRAPHS:
        dot = _read(out_dir / "graphs" / f"{kind}.dot")
        if dot is None:
            continue
        svg, reason = _render_graph(dot, max_graph_nodes)
        if svg is None:
            report.omitted.append(f"graphs/{kind}: {reason}")
            graph_html.append(f'<figure><figcaption>{_e(caption)}</figcaption><p class="note">{_e(reason)}。</p></figure>')
        else:
            report.graphs.append(kind)
            graph_html.append(f'<figure class="cap"><figcaption>図: {_e(caption)}（実線は確定、破線は推定、点線は未解決・外部）</figcaption>{svg}</figure>')

    for title, items in SECTIONS:
        blocks, subs = [], []
        for name, caption in items:
            identifier, block = file_block(name, caption)
            blocks.append(block)
            subs.append((identifier, name))
        extra = ""
        if title == "全体像" and graph_html:
            extra = f'<h2 id="graphs">図</h2>{"".join(graph_html)}'
            subs.append(("graphs", "図"))
        chapter(title, "".join(blocks) + extra, subs)
        if title == "入口と境界":  # 「処理を追う」は、図の後・関数のカードを、入口と境界の次に置く
            cards: list[str] = []
            card_subs: list[tuple[str, str]] = []
            for path in sorted((out_dir / "functions").glob("*.txt")) if (out_dir / "functions").is_dir() else []:
                identifier = f"fn-{_slug(path.stem)}"
                text = _read(path) or ""
                cards.append(f'<h3 id="{identifier}">{_e(path.stem)}</h3>{_text_block(text, max_lines, f"functions/{path.name}", report.omitted)}')
                card_subs.append((identifier, path.stem))
            no_cards = '<p class="note">読解カードがありません。</p>'
            intro = '<p class="muted" id="functions">入口・よく呼ばれる・多くを呼ぶ・大きい関数から選んだカードです。「誰が呼ぶか」「何を入力するか」「何を変更するか」「失敗するとどうなるか」を、確認できた事実で示します。</p>'
            chapter("処理を追う（主要な関数の読解カード）", intro + ("".join(cards) or no_cards), card_subs)

    # --- AI解説（あれば） ---
    ai_dir = out_dir / "ai"
    if ai_dir.is_dir() and any(ai_dir.iterdir()):
        explained, ai_subs, summary_rows = [], [], []
        for path in sorted(ai_dir.glob("*.md")):
            identifier = f"ai-{_slug(path.stem)}"
            text = _read(path) or ""
            explained.append(f'<h3 id="{identifier}">{_e(path.stem)}</h3>{_text_block(text, max_lines, f"ai/{path.name}", report.omitted)}')
            ai_subs.append((identifier, path.stem))
            summary_rows.append((identifier, path.stem, *_ai_summary(text)))
        counts = {label: sum(1 for row in summary_rows if row[3] == label) for label in ("検証済み", "一部未確認", "未検証")}
        table = (
            "<h2>検証状態の一覧</h2><table><tr><th>対象</th><th>モデル</th><th>検証状態</th><th>引用（検証できた/全体）</th></tr>"
            + "".join(f'<tr><td><a href="#{i}">{_e(n)}</a></td><td>{_e(m)}</td><td>{_e(v)}</td><td>{_e(c)}</td></tr>' for i, n, m, v, c in summary_rows)
            + "</table>"
            + f'<p class="muted">検証済み {counts["検証済み"]} 件 / 一部未確認 {counts["一部未確認"]} 件 / 未検証 {counts["未検証"]} 件'
            "（検証できるのは、示された根拠が存在し、渡した範囲内であることまで。根拠が主張を実際に裏付けているかは、読む人が確認してください）。</p>"
        )
        intro = (
            '<p class="ai"><strong>AIが解析結果を入力に生成した解説で、解析結果（事実）ではありません。</strong>'
            "引用の検証結果に注意して読んでください。<strong>「未検証」の解説は、事実として扱わないでください</strong>（引用の誤り・存在しない名前・根拠なしを含みます）。"
            "「⚠未確認」と付いた行は、根拠が示されていない記述です。</p>"
        )
        chapter("AIの解説", intro + table + "".join(explained), ai_subs)

    # --- 付録 ---
    logs = sorted(p for p in (out_dir / "logs").glob("*") if p.is_file()) if (out_dir / "logs").is_dir() else []
    appendix = "<h2>用語</h2><table><tr><th>表示</th><th>意味</th></tr>" + "".join(
        f"<tr><td>{_e(a)}</td><td>{_e(b)}</td></tr>" for a, b in (
            ("解決（確定）", "プロジェクト内の定義に、静的に確定して対応づけられた"), ("解決（推定）", "候補は特定できたが、実行時の挙動で変わりうる"),
            ("曖昧", "候補が複数あり、1つに決められない"), ("未解決", "静的に確定できない（関数ポインタ・動的呼び出しなど）"),
            ("外部", "標準ライブラリ・外部ライブラリなど、プロジェクト外"), ("手がかり", "バグの断定ではなく、確認すべき箇所の候補"),
        )) + "</table>"
    if logs:
        appendix += "<h2>取得時の警告・エラー</h2>" + "".join(
            f"<h3>{_e(p.name)}</h3>{_text_block(_read(p) or '', 40, 'logs/' + p.name, report.omitted)}" for p in logs
        )
    if report.omitted:
        appendix += "<h2>省略・打ち切りしたもの</h2><ul>" + "".join(f"<li>{_e(item)}</li>" for item in report.omitted) + "</ul>"
    chapter("付録", appendix)

    toc_html = "<h1>目次</h1><div class=\"toc\"><ul>" + "".join(
        f'<li><a href="#{i}">{n}. {_e(t)}</a>' + (
            "<ul>" + "".join(f'<li><a href="#{si}">{_e(st)}</a></li>' for si, st in subs[:40]) + (f'<li class="muted">… ほか {len(subs) - 40} 件</li>' if len(subs) > 40 else "") + "</ul>" if subs else ""
        ) + "</li>"
        for n, (i, t, subs) in enumerate(toc, 1)
    ) + "</ul></div>"
    csp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"  # スクリプト・外部通信を遮断する
    report.html = (
        f'<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{csp}">'
        f"<title>コードリーディング資料: {_e(project)}</title><style>{CSS}</style></head><body>{cover}{toc_html}{''.join(body)}</body></html>"
    )
    return report
