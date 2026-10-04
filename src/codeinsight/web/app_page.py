"""動的なWebビューアーのページ。グラフ・ソース・切り出しを、APIから取得して表示する。

APIから得た文字列（シンボル名・ソース・パス）は、解析対象由来の信頼できない入力なので、DOMへは textContent のみで反映し、
innerHTML は使わない。トークンは、ページに埋め込み、APIへのリクエストのヘッダーで送る（URLには残さない）。
"""

from __future__ import annotations

import json

from codeinsight.presentation.html_viewer import render_template

_CONTROLS = """<div id="controls">
  <strong id="project-name"></strong><span id="project-stats" class="notes"></span>
  <input id="q" list="symbol-list" type="search" placeholder="シンボルを検索（関数・クラス）" aria-label="シンボルを検索" size="34">
  <datalist id="symbol-list"></datalist>
  <select id="kind" aria-label="グラフの種類">
    <option value="call">呼び出し</option><option value="flow">制御フロー</option><option value="deps">ファイル依存</option>
    <option value="inherit">継承</option><option value="arch">アーキテクチャ</option>
  </select>
  <select id="direction" aria-label="方向">
    <option value="both">両方（Depends On / Depended On By）</option><option value="out">Depends On（出る）</option><option value="in">Depended On By（入る）</option>
  </select>
  <label>深さ <input id="depth" type="number" min="0" max="10" value="2" style="width:4em"></label>
  <button id="draw" type="button">描画</button>
  <span id="message" class="notes" role="status"></span>
</div>"""

_READER = """<section id="reader" aria-label="コードリーディング">
  <div class="tabs">
    <button id="tab-source" type="button" class="active">ソース</button>
    <button id="tab-extract" type="button">切り出し</button>
    <span id="reader-title" class="notes"></span>
  </div>
  <div id="pane-source"><pre id="source" aria-label="選択したノードのソース"></pre></div>
  <div id="pane-extract" hidden>
    <label>範囲 <select id="x-direction"><option value="callees">呼び出し先</option><option value="callers">呼び出し元</option><option value="both">両方</option></select></label>
    <label>深さ <input id="x-depth" type="number" min="0" max="6" value="1" style="width:4em"></label>
    <button id="x-run" type="button">選択中の関数を起点に切り出す</button>
    <button id="x-save" type="button" disabled>Markdownを保存</button>
    <pre id="x-out" aria-label="切り出し結果"></pre>
  </div>
</section>"""

_BOOT = r"""
(function () {
  "use strict";
  var TOKEN = __TOKEN__;
  history.replaceState(null, "", location.pathname);
  document.body.classList.add("app");
  var $ = function (id) { return document.getElementById(id); };
  var selected = null, lastMarkdown = "";

  function api(path, params) {
    var query = Object.keys(params || {}).filter(function (k) { return params[k] !== "" && params[k] !== null && params[k] !== undefined; })
      .map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
    return fetch(path + (query ? "?" + query : ""), { headers: { "X-CodeInsight-Token": TOKEN }, cache: "no-store" }).then(function (r) {
      return r.json().then(function (body) { if (!r.ok) { var e = new Error(body.error || ("HTTP " + r.status)); e.body = body; throw e; } return body; });
    });
  }
  function say(text) { $("message").textContent = text || ""; }

  function showSource(node) {
    if (!node.path) { $("source").textContent = "（ソースの位置を持たないノードです）"; return; }
    var isBlock = node.kind === "block" || node.kind === "decision" || node.kind === "terminal";
    var params = { path: node.path };
    if (node.line) {
      params.start = node.line;
      params.end = (!isBlock && node.end_line) ? node.end_line : node.line;
      params.context = isBlock ? 6 : 0;
    }
    api("/api/source", params).then(function (data) {
      var pre = $("source"); pre.textContent = "";
      $("reader-title").textContent = data.path + (node.line ? ":" + node.line : "") + (data.freshness !== "fresh" ? "  ※解析後に変更されています。行の位置が対応しない可能性があります" : "");
      data.lines.forEach(function (l) {
        var row = document.createElement("div");
        row.className = "src-line" + (l.n >= data.highlight_start && l.n <= data.highlight_end && data.highlight_start ? " hl" : "");
        var n = document.createElement("span"); n.className = "n"; n.textContent = String(l.n);
        var t = document.createElement("span"); t.textContent = l.text;
        row.appendChild(n); row.appendChild(t); pre.appendChild(row);
      });
      if (data.truncated) { var more = document.createElement("div"); more.textContent = "…（行数の上限で打ち切り）"; pre.appendChild(more); }
    }).catch(function (e) { $("source").textContent = e.message; });
  }

  function onNode(node) {
    selected = node;
    $("reader-title").textContent = node.label;
    showSource(node);
  }

  function draw() {
    var kind = $("kind").value, root = $("q").value.trim();
    var params = { kind: kind, direction: $("direction").value, depth: $("depth").value };
    if (root) params.root = root;
    say("読み込み中…");
    api("/api/graph", params).then(function (data) {
      say(data.nodes.length + " ノード / " + data.edges.length + " 辺");
      window.renderGraph(data, { onNode: onNode });
    }).catch(function (e) {
      var detail = e.body && e.body.candidates ? "（候補: " + e.body.candidates.slice(0, 5).map(function (c) { return c.qualified_name; }).join(", ") + "）" : "";
      say(e.message + detail);
    });
  }

  $("draw").onclick = draw;
  $("q").onkeydown = function (ev) { if (ev.key === "Enter") draw(); };
  var timer = null;
  $("q").oninput = function () {
    clearTimeout(timer);
    var value = $("q").value.trim();
    if (value.length < 2) return;
    timer = setTimeout(function () {
      api("/api/symbols", { q: value, limit: 20 }).then(function (data) {
        var list = $("symbol-list"); list.textContent = "";
        data.symbols.forEach(function (s) { var o = document.createElement("option"); o.value = s.qualified_name; o.label = s.kind + "  " + s.path + ":" + s.start_line; list.appendChild(o); });
      }).catch(function () {});
    }, 200);
  };
  function tab(name) {
    $("pane-source").hidden = name !== "source"; $("pane-extract").hidden = name !== "extract";
    $("tab-source").classList.toggle("active", name === "source"); $("tab-extract").classList.toggle("active", name === "extract");
  }
  $("tab-source").onclick = function () { tab("source"); };
  $("tab-extract").onclick = function () { tab("extract"); };
  $("x-run").onclick = function () {
    var root = selected && selected.kind !== "block" && selected.kind !== "decision" && selected.kind !== "terminal" ? selected.label : $("q").value.trim();
    if (!root) { $("x-out").textContent = "グラフのノードを選ぶか、シンボルを入力してください。"; return; }
    $("x-out").textContent = "読み込み中…";
    api("/api/extract", { root: root, direction: $("x-direction").value, depth: $("x-depth").value }).then(function (data) {
      lastMarkdown = data.markdown; $("x-out").textContent = data.markdown; $("x-save").disabled = false;
    }).catch(function (e) { $("x-out").textContent = e.message; $("x-save").disabled = true; });
  };
  $("x-save").onclick = function () {
    var blob = new Blob([lastMarkdown], { type: "text/markdown;charset=utf-8" });
    var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "extract.md"; a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  };

  api("/api/project", {}).then(function (info) {
    $("project-name").textContent = info.name;
    $("project-stats").textContent = "  ファイル " + info.files + " / シンボル " + info.symbols + (info.stale_count ? " / 解析後に変更されたファイル " + info.stale_count + "件" : "");
    $("kind").value = "arch"; draw();
  }).catch(function (e) { say(e.message); });
})();
"""


def content_security_policy(nonce: str, header: bool = True) -> str:
    """ページのCSP。HTTPヘッダーでは frame-ancestors も付ける（<meta> では無効のため、metaには付けない）。"""

    policy = f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
    return policy + ("; frame-ancestors 'none'" if header else "")


def render_app(token: str, nonce: str) -> str:
    csp = content_security_policy(nonce, header=False)
    payload = json.dumps(token).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return render_template(csp, _CONTROLS, _READER, nonce, _BOOT.replace("__TOKEN__", payload))
