"""動的なWebビューアーのページ。グラフ・ソース・切り出しを、APIから取得して表示する。

APIから得た文字列（シンボル名・ソース・パス）は、解析対象由来の信頼できない入力なので、DOMへは textContent のみで反映し、
innerHTML は使わない。トークンは、ページに埋め込み、APIへのリクエストのヘッダーで送る（URLには残さない）。
"""

from __future__ import annotations

import json

from codeinsight.presentation.html_viewer import render_template

_CONTROLS = """<div id="controls">
  <strong id="project-name"></strong><span id="project-stats" class="notes"></span>
  <span class="viewtabs" role="tablist" aria-label="表示の切り替え">
    <button id="view-graph" type="button" role="tab" class="active">グラフ</button>
    <button id="view-reading" type="button" role="tab">資料</button>
    <button id="view-unresolved" type="button" role="tab">未解決</button>
    <button id="view-explore" type="button" role="tab">探索</button>
    <button id="view-status" type="button" role="tab">状態</button>
    <button id="view-help" type="button" role="tab">使い方</button>
  </span>
  <span id="graph-controls">
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
  </span>
  <span id="message" class="notes" role="status"></span>
</div>
<div id="docs-view" hidden>
  <nav id="docs-nav" aria-label="資料の一覧"></nav>
  <article id="docs-body" tabindex="0"></article>
</div>"""

_READER = """<div id="splitter" role="separator" aria-orientation="horizontal" tabindex="0" aria-label="グラフとソースの境界。ドラッグ、または上下キーで高さを変更。ダブルクリックで元に戻す"></div>
<section id="reader" aria-label="コードリーディング">
  <div class="tabs">
    <button id="tab-source" type="button" class="active">ソース</button>
    <button id="tab-extract" type="button">切り出し</button>
    <button id="tab-card" type="button">読解カード</button>
    <span id="reader-title" class="notes"></span>
  </div>
  <div id="pane-source"><pre id="source" aria-label="選択したノードのソース"></pre></div>
  <div id="pane-card" hidden>
    <div class="x-controls">
    <button id="c-run" type="button">選択中の関数の読解カード</button>
    <span class="hint">確定・推定・未解決は、項目ごとの印で区別します。</span>
    </div>
    <div id="card-out" class="card" aria-label="読解カード"></div>
  </div>
  <div id="pane-extract" hidden>
    <div class="x-controls">
    <label>範囲 <select id="x-direction"><option value="callees">呼び出し先</option><option value="callers">呼び出し元</option><option value="both">両方</option></select></label>
    <label>深さ <input id="x-depth" type="number" min="0" max="6" value="1" style="width:4em"></label>
    <button id="x-run" type="button">選択中の関数を起点に切り出す</button>
    <button id="x-save" type="button" disabled>Markdownを保存</button>
    </div>
    <div id="x-out" class="md-out" aria-label="切り出し結果"></div>
  </div>
</section>"""

_BOOT = r"""
(function () {
  "use strict";
  var TOKEN = __TOKEN__;
  // URLの # 以降（#view=reading&doc=README.md&kind=call&root=…&select=…&tab=extract）で、表示する内容を指定できる（操作説明の画像の生成にも使う）
  var HASH = {};
  location.hash.replace(/^#/, "").split("&").forEach(function (part) {
    var i = part.indexOf("="); if (i > 0) { try { HASH[decodeURIComponent(part.slice(0, i))] = decodeURIComponent(part.slice(i + 1)); } catch (e) { /* 不正な指定は無視する */ } }
  });
  history.replaceState(null, "", location.pathname);
  document.body.classList.add("app");

  // グラフとソース・切り出しの境界を、ドラッグ（または上下キー）で動かす。高さは、このブラウザに記憶する（使えなければ記憶しない）。
  var splitter = document.getElementById("splitter"), reader = document.getElementById("reader"), MIN_READER = 80, KEY = "codeinsight.readerHeight";
  function setReaderHeight(px, save) {
    var h = Math.round(Math.max(MIN_READER, Math.min(window.innerHeight - 260, px)));
    reader.style.setProperty("--reader-h", h + "px");
    document.documentElement.style.setProperty("--reader-h", h + "px");
    if (save) { try { localStorage.setItem(KEY, String(h)); } catch (e) { /* 記憶できなくても動作する */ } }
    return h;
  }
  try { var stored = parseInt(localStorage.getItem(KEY), 10); if (stored > 0) setReaderHeight(stored, false); } catch (e) { /* 記憶は任意 */ }
  splitter.onpointerdown = function (ev) {
    ev.preventDefault();
    try { splitter.setPointerCapture(ev.pointerId); } catch (e) { /* キャプチャできなくても、ドラッグは動作する */ }
    splitter.classList.add("dragging");
  };
  splitter.onpointermove = function (ev) {
    if (!splitter.classList.contains("dragging")) return;
    setReaderHeight(window.innerHeight - ev.clientY - 4, false);
  };
  splitter.onpointerup = function (ev) {
    splitter.classList.remove("dragging");
    try { splitter.releasePointerCapture(ev.pointerId); } catch (e) { /* 既に解放済み */ }
    setReaderHeight(reader.getBoundingClientRect().height, true);
  };
  splitter.onkeydown = function (ev) {
    var step = ev.shiftKey ? 120 : 24, now = reader.getBoundingClientRect().height;
    if (ev.key === "ArrowUp") { ev.preventDefault(); setReaderHeight(now + step, true); }
    else if (ev.key === "ArrowDown") { ev.preventDefault(); setReaderHeight(now - step, true); }
  };
  splitter.ondblclick = function () {
    reader.style.removeProperty("--reader-h"); document.documentElement.style.removeProperty("--reader-h");
    try { localStorage.removeItem(KEY); } catch (e) { /* 記憶は任意 */ }
  };
  var $ = function (id) { return document.getElementById(id); };
  var selected = null, lastMarkdown = "", drawCount = 0;

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
        row.setAttribute("data-line", String(l.n));
        var mk = document.createElement("span"); mk.className = "mk";
        var n = document.createElement("span"); n.className = "n"; n.textContent = String(l.n);
        var t = document.createElement("span"); t.textContent = l.text;
        row.appendChild(mk); row.appendChild(n); row.appendChild(t); pre.appendChild(row);
      });
      if (data.truncated) { var more = document.createElement("div"); more.textContent = "…（行数の上限で打ち切り）"; pre.appendChild(more); }
      applyMarks(data.path);
    }).catch(function (e) { $("source").textContent = e.message; });
  }

  function onNode(node) {
    selected = node;
    // 関数・クラスを選んだら、上部の入力にも反映する（種類を「制御フロー」などに切り替えると、その関数が起点になる）
    if (["function", "method", "class"].indexOf(node.kind) >= 0) $("q").value = node.label;
    $("reader-title").textContent = node.label;
    showSource(node);
    if (!$("pane-card").hidden && ["function", "method"].indexOf(node.kind) >= 0) loadCard(node.label);  // 読解カードを開いているときは、選んだ関数のカードに切り替える
  }

  function draw() {
    var kind = $("kind").value, root = $("q").value.trim();
    var params = { kind: kind, direction: $("direction").value, depth: $("depth").value };
    if (root) params.root = root;
    // 古いリクエストの応答が、新しい描画を上書きしないようにする
    var ticket = ++drawCount;
    say("読み込み中…");
    return api("/api/graph", params).then(function (data) {
      if (ticket !== drawCount) return;
      say(data.nodes.length + " ノード / " + data.edges.length + " 辺");
      window.renderGraph(data, { onNode: onNode });
    }).catch(function (e) {
      if (ticket !== drawCount) return;
      var detail = e.body && e.body.candidates ? "（候補: " + e.body.candidates.slice(0, 5).map(function (c) { return c.qualified_name; }).join(", ") + "）" : "";
      say(e.message + detail);
    });
  }

  // ---- 資料・使い方（Markdown・テキストの閲覧） ----
  var PATH_LINE = /([A-Za-z0-9_][A-Za-z0-9_.\/+-]*\.(?:py|pyi|c|h|cc|cpp|cxx|hpp|hh|hxx|el|org|go)):(\d+)(?:-(\d+))?/g;
  var docs = { files: [], current: null, imageUrls: {}, guideImages: [] };

  function showSourceAt(path, start, end) {
    tab("source");
    $("reader-title").textContent = path + ":" + start;
    showSource({ path: path, line: start, end_line: end || start, kind: "function", label: path });
  }
  function el(tag, text, cls) { var e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = text; if (cls) e.className = cls; return e; }
  // テキストの中の `ファイル:行` を、ソース表示へのリンクにする（DOMのみで組み立てる）
  function appendLinked(parent, text) {
    var last = 0, m, re = new RegExp(PATH_LINE.source, "g");  // 状態（lastIndex）を持つため、呼び出しごとに作る
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      var a = el("a", m[0], "srclink"); a.href = "#"; a.setAttribute("role", "button");
      (function (p, s, e) { a.onclick = function (ev) { ev.preventDefault(); showSourceAt(p, parseInt(s, 10), e ? parseInt(e, 10) : null); }; })(m[1], m[2], m[3]);
      parent.appendChild(a); last = m.index + m[0].length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }
  var INLINE = /(`[^`]+`)|(!\[[^\]]*\]\([^)\s]+\))|(\[[^\]]+\]\([^)\s]+\))|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)/g;
  function inline(parent, text, base) {
    var last = 0, m, re = new RegExp(INLINE.source, "g");  // 再帰（強調の中の強調など）でも、走査の位置が壊れないよう、呼び出しごとに作る
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) appendLinked(parent, text.slice(last, m.index));
      var t = m[0];
      if (m[1]) { var c = el("code"); appendLinked(c, t.slice(1, -1)); parent.appendChild(c); }
      else if (m[2]) { var im = /^!\[([^\]]*)\]\(([^)\s]+)\)$/.exec(t); parent.appendChild(image(im[1], im[2])); }
      else if (m[3]) { var lm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(t); parent.appendChild(link(lm[1], lm[2], base)); }
      else if (m[4]) { var b = el("strong"); inline(b, t.slice(2, -2), base); parent.appendChild(b); }
      else { var i = el("em"); inline(i, t.slice(1, -1), base); parent.appendChild(i); }
      last = m.index + t.length;
    }
    if (last < text.length) appendLinked(parent, text.slice(last));
  }
  function link(text, href, base) {
    var a = el("a", text); a.href = "#";
    if (href.charAt(0) === "#") {
      a.onclick = function (ev) { ev.preventDefault(); var t = document.getElementById("doc-" + href.slice(1)); if (t) t.scrollIntoView(); };
    } else if (/^https?:/i.test(href)) {
      a.removeAttribute("href"); a.textContent = text + "（" + href + "）";   // 外部のURLは、リンクにしない（資料の内容は、信頼できない入力）
    } else {
      var target = (base ? base.replace(/[^/]*$/, "") : "") + href.replace(/^\.\//, "");
      a.onclick = function (ev) { ev.preventDefault(); openDoc(target); };
    }
    return a;
  }
  function image(alt, src) {
    var img = el("img"); img.alt = alt; img.className = "doc-image";
    var name = src.replace(/^.*\//, "");
    if (docs.guideImages.indexOf(name) >= 0) {
      if (docs.imageUrls[name]) img.src = docs.imageUrls[name];
      else fetchBlob("/api/guide/image", { name: name }).then(function (url) { docs.imageUrls[name] = url; img.src = url; }).catch(function () {});
    }
    return img;
  }
  function fetchBlob(path, params) {
    var query = Object.keys(params).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
    return fetch(path + "?" + query, { headers: { "X-CodeInsight-Token": TOKEN }, cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status); return r.blob();
    }).then(function (b) { return URL.createObjectURL(b); });
  }
  function slug(text) { return text.toLowerCase().replace(/[^\w\u3040-\u30ff\u4e00-\u9fff]+/g, "-").replace(/^-|-$/g, ""); }

  // Markdown: 見出し・段落・箇条書き・表・コードブロック・引用・水平線・インライン（コード・強調・リンク・画像）のみ。HTMLは解釈しない。
  function renderMarkdown(container, text, base) {
    container.textContent = "";
    var lines = text.replace(/\r\n?/g, "\n").split("\n"), i = 0, headings = [];
    function isTableSep(l) { return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l || ""); }
    function cells(l) { return l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (c) { return c.trim(); }); }
    while (i < lines.length) {
      var line = lines[i], m;
      if (/^\s*$/.test(line)) { i++; continue; }
      if ((m = /^(`{3,}|~{3,})(.*)$/.exec(line))) {
        var fence = m[1], body = []; i++;
        while (i < lines.length && lines[i].indexOf(fence) !== 0) { body.push(lines[i]); i++; }
        i++;
        var pre = el("pre", null, "doc-code"), code = el("code"); appendLinked(code, body.join("\n")); pre.appendChild(code); container.appendChild(pre); continue;
      }
      if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
        var h = el("h" + m[1].length); h.id = "doc-" + slug(m[2]); inline(h, m[2], base); container.appendChild(h);
        if (m[1].length <= 2) headings.push({ id: h.id, text: m[2], level: m[1].length });
        i++; continue;
      }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { container.appendChild(el("hr")); i++; continue; }
      if (line.indexOf("|") >= 0 && isTableSep(lines[i + 1])) {
        var table = el("table", null, "doc-table"), head = el("tr");
        cells(line).forEach(function (c) { var th = el("th"); inline(th, c, base); head.appendChild(th); });
        var thead = el("thead"); thead.appendChild(head); table.appendChild(thead);
        var tbody = el("tbody"); i += 2;
        while (i < lines.length && lines[i].indexOf("|") >= 0 && !/^\s*$/.test(lines[i])) {
          var tr = el("tr"); cells(lines[i]).forEach(function (c) { var td = el("td"); inline(td, c, base); tr.appendChild(td); }); tbody.appendChild(tr); i++;
        }
        table.appendChild(tbody); container.appendChild(table); continue;
      }
      if (/^\s*>/.test(line)) {
        var quote = el("blockquote"), qtext = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { qtext.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
        var qp = el("p"); inline(qp, qtext.join(" "), base); quote.appendChild(qp); container.appendChild(quote); continue;
      }
      if ((m = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(line))) {
        var ordered = /\d/.test(m[2]), list = el(ordered ? "ol" : "ul"), item = null;
        while (i < lines.length && (m = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(lines[i]))) {
          var li = el("li"); inline(li, m[3], base);
          if (m[1].length >= 2 && item) { var sub = item.lastChild && item.lastChild.tagName === "UL" ? item.lastChild : item.appendChild(el("ul")); sub.appendChild(li); }
          else { list.appendChild(li); item = li; }
          i++;
        }
        container.appendChild(list); continue;
      }
      var para = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#{1,6}\s|`{3,}|~{3,}|\s*>|\s*([-*+]|\d+\.)\s)/.test(lines[i]) && !(lines[i].indexOf("|") >= 0 && isTableSep(lines[i + 1]))) { para.push(lines[i]); i++; }
      var p = el("p"); inline(p, para.join(" "), base); container.appendChild(p);
    }
    return headings;
  }

  function renderDoc(entry, text) {
    var body = $("docs-body");
    body.scrollTop = 0;
    if (entry.kind === "markdown") { renderMarkdown(body, text, entry.name); return; }
    body.textContent = "";
    var pre = el("pre", null, "doc-code"); appendLinked(pre, text); body.appendChild(pre);
  }
  function openDoc(name) {
    var entry = docs.files.filter(function (f) { return f.name === name; })[0];
    if (!entry) { say("資料が見つかりません: " + name); return; }
    docs.current = name;
    [].forEach.call($("docs-nav").querySelectorAll("button"), function (b) { b.classList.toggle("active", b.getAttribute("data-name") === name); });
    api("/api/reading/file", { name: name }).then(function (data) { renderDoc(entry, data.text); }).catch(function (e) { $("docs-body").textContent = e.message; });
  }
  function buildNav(files) {
    var nav = $("docs-nav"); nav.textContent = ""; var group = null;
    files.forEach(function (f) {
      if (f.group !== group) { group = f.group; nav.appendChild(el("h3", group)); }
      var b = el("button", f.title); b.type = "button"; b.setAttribute("data-name", f.name); b.onclick = function () { openDoc(f.name); }; nav.appendChild(b);
    });
  }
  function showView(name) {
    document.body.classList.toggle("view-docs", name !== "graph");
    ["graph", "reading", "unresolved", "explore", "status", "help"].forEach(function (v) { $("view-" + v).classList.toggle("active", v === name); });
    $("docs-view").hidden = name === "graph";
    $("graph-controls").hidden = name !== "graph";
    $("docs-body").classList.remove("wide");
    if (name === "unresolved") { showUnresolved(); return; }
    if (name === "explore") { showExplore(HASH.explore); return; }
    if (name === "status") { showStatus(); return; }
    if (name === "reading") {
      return api("/api/reading", {}).then(function (data) {
        docs.files = data.files; buildNav(data.files);
        if (!data.available || !data.files.length) { $("docs-body").textContent = data.message || "資料がありません。make reading で作成してください。"; return; }
        openDoc(docs.current || HASH.doc || data.files[0].name);
      }).catch(function (e) { $("docs-body").textContent = e.message; });
    } else if (name === "help") {
      return api("/api/guide", {}).then(function (data) {
        var nav = $("docs-nav"); nav.textContent = "";
        if (!data.available) { $("docs-body").textContent = "使い方の資料がありません。"; return; }
        docs.guideImages = data.images;
        var headings = renderMarkdown($("docs-body"), data.text, "");
        nav.appendChild(el("h3", "使い方"));
        headings.forEach(function (h) { var b = el("button", h.text); b.type = "button"; b.onclick = function () { var t = document.getElementById(h.id); if (t) t.scrollIntoView(); }; nav.appendChild(b); });
      }).catch(function (e) { $("docs-body").textContent = e.message; });
    }
  }
  $("view-graph").onclick = function () { showView("graph"); };
  $("view-reading").onclick = function () { showView("reading"); };
  $("view-help").onclick = function () { showView("help"); };
  $("view-unresolved").onclick = function () { showView("unresolved"); };
  $("view-explore").onclick = function () { showView("explore"); };
  $("view-status").onclick = function () { showView("status"); };

  // ---- 状態の印（確定・推定・未解決・曖昧・外部・観測・AIの推測）。色だけでなく、記号と文字でも区別する ----
  var BADGES = {
    confirmed: ["✓", "確定"], resolved: ["✓", "確定"], inferred: ["~", "推定"], unresolved: ["?", "未解決"], ambiguous: ["◇", "曖昧"],
    external: ["⌂", "外部"], observed: ["●", "観測"], ai: ["✦", "AIの推測"],
    verified: ["✓", "検証済み"], partial: ["△", "一部未確認"], unverified: ["✗", "未検証"]
  };
  function badge(kind) {
    var b = BADGES[kind]; if (!b) return document.createTextNode("");
    return el("span", b[0] + " " + b[1], "badge b-" + kind);
  }
  function resolutionKind(resolution, confidence) {
    if (resolution === "resolved") return confidence === "inferred" ? "inferred" : "confirmed";
    return resolution || "confirmed";
  }

  // ---- ソースの欄外の印（解析結果に保存された事実だけ。? 未解決 / ◇ 曖昧 / ~ 推定） ----
  var marksCache = {};
  var MARK_SYMBOL = { unresolved: "?", ambiguous: "◇", inferred: "~" };
  var MARK_TEXT = { unresolved: "未解決の呼び出し", ambiguous: "曖昧な呼び出し（候補が複数）", inferred: "推定で解決した呼び出し（確定ではない）" };
  function applyMarks(path) {
    var fetchMarks = marksCache[path] ? Promise.resolve(marksCache[path]) : api("/api/marks", { path: path }).then(function (d) { marksCache[path] = d; return d; });
    fetchMarks.then(function (data) {
      var rows = {};
      [].forEach.call($("source").querySelectorAll(".src-line"), function (r) { rows[r.getAttribute("data-line")] = r; });
      data.marks.forEach(function (m) {
        var row = rows[String(m.line)]; if (!row) return;
        var mk = row.querySelector(".mk"); if (!mk || mk.textContent) return;
        mk.textContent = MARK_SYMBOL[m.kind]; mk.className = "mk mk-" + m.kind; mk.title = MARK_TEXT[m.kind] + ": " + m.label;
        mk.tabIndex = 0; mk.setAttribute("role", "button");
        var toggle = function () {
          var next = row.nextSibling;
          if (next && next.className === "mark-detail") { next.remove(); return; }
          var detail = el("div", null, "mark-detail");
          detail.appendChild(badge(m.kind)); detail.appendChild(el("strong", " " + m.label)); detail.appendChild(document.createTextNode("  （" + m.function + "）"));
          if (m.detail) { var note = el("div"); appendLinked(note, m.detail); detail.appendChild(note); }
          var open = el("button", "この関数の読解カード"); open.type = "button"; open.onclick = function () { openCard(m.function); };
          detail.appendChild(open);
          row.parentNode.insertBefore(detail, row.nextSibling);
        };
        mk.onclick = toggle; mk.onkeydown = function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); } };
      });
      if (data.stale) $("reader-title").textContent += "  ※印は、解析時の位置です（解析後に変更されています）";
    }).catch(function () { /* 印は補助。取れなくても、ソースは読める */ });
  }

  // ---- 読解カード（8つの問い）。項目ごとに、確定・推定・未解決の印を付ける ----
  var cardSymbol = null;
  function section(parent, title, sub) {
    var h = el("h2", title); if (sub) { h.appendChild(document.createTextNode(" ")); h.appendChild(el("small", sub)); }
    parent.appendChild(h); var box = el("div"); parent.appendChild(box); return box;
  }
  function renderAny(parent, value, depth) {
    depth = depth || 0;
    if (value === null || value === undefined || value === "") return;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") { var p = el("div"); appendLinked(p, String(value)); parent.appendChild(p); return; }
    if (Array.isArray(value)) {
      if (!value.length) return;
      var ul = el("ul");
      value.slice(0, 30).forEach(function (item) {
        var li = el("li");
        if (item && typeof item === "object" && !Array.isArray(item)) {
          if (item.resolution) li.appendChild(badge(resolutionKind(item.resolution, item.confidence)));
          var parts = Object.keys(item).filter(function (k) { return k !== "resolution" && k !== "confidence" && item[k] !== null && item[k] !== "" && typeof item[k] !== "object"; })
            .map(function (k) { return k + ": " + item[k]; });
          appendLinked(li, parts.join(" / "));
          if (depth < 2) Object.keys(item).forEach(function (k) { if (item[k] && typeof item[k] === "object") { var w = el("div"); w.appendChild(el("small", k + ":")); renderAny(w, item[k], depth + 1); li.appendChild(w); } });
        } else { appendLinked(li, String(item)); }
        ul.appendChild(li);
      });
      if (value.length > 30) ul.appendChild(el("li", "…ほか " + (value.length - 30) + "件"));
      parent.appendChild(ul); return;
    }
    if (typeof value === "object") {
      Object.keys(value).forEach(function (k) {
        var v = value[k]; if (v === null || v === "" || (Array.isArray(v) && !v.length) || k === "symbol") return;
        var row = el("div", null, "kv"); row.appendChild(el("b", k)); var cell = el("span");
        if (typeof v === "object") { renderAny(cell, v, depth + 1); } else { appendLinked(cell, String(v)); }
        row.appendChild(cell); parent.appendChild(row);
      });
    }
  }
  function renderCard(card) {
    var out = $("card-out"); out.textContent = "";
    var head = el("h2", card.symbol.qualified_name); head.appendChild(document.createTextNode(" ")); head.appendChild(el("small", card.symbol.kind + "  " + card.path + ":" + card.symbol.start_line + "-" + card.symbol.end_line + "  [" + card.language + "]"));
    out.appendChild(head);
    var srcBtn = el("button", "ソースを開く"); srcBtn.type = "button"; srcBtn.onclick = function () { showSourceAt(card.path, card.symbol.start_line, card.symbol.end_line); };
    out.appendChild(srcBtn);
    if (card.summary) out.appendChild(el("p", card.summary));
    if (card.limitations && card.limitations.length) {
      var lim = el("div", null, "lim"); lim.appendChild(el("strong", "確認できなかったこと・対応範囲外")); renderAny(lim, card.limitations); out.appendChild(lim);
    }
    var q1 = section(out, "1. なぜ存在するのか", "手がかり。理由そのものはコードから確認できません");
    renderAny(q1, { module_summary: card.module_summary, test_names: (card.test_names || []).slice(0, 5), doc_mentions: card.doc_mentions, todo_comments: card.todo_comments });
    var q2 = section(out, "2. 誰が呼ぶのか", "呼び出し元 " + (card.caller_total || 0) + "件（解決済みの呼び出しのみ）");
    renderAny(q2, card.callers); renderAny(q2, card.registrations);
    var q3 = section(out, "3. 何を入力するのか");
    renderAny(q3, card.parameters); renderAny(q3, { "呼び出し元が渡す値": card.caller_arguments, "設定値": card.config_reads, "外部からの入力": card.external_inputs });
    var q4 = section(out, "4. 何を変更するのか");
    renderAny(q4, card.state_changes); renderAny(q4, card.parameter_mutations); renderAny(q4, card.effects);
    var q5 = section(out, "5. 何を返すのか");
    renderAny(q5, { "戻り値の型": card.return_annotation, "return の行": (card.returns || []).map(function (r) { return r.line || r[0] || r; }).join(", "), "ジェネレータ": card.is_generator ? "はい" : "", "async": card.is_async ? "はい" : "", "備考": card.return_note });
    var q6 = section(out, "6. 誰に影響するのか");
    renderAny(q6, card.impact); renderAny(q6, card.tests);
    var q7 = section(out, "7. 失敗するとどうなるのか");
    renderAny(q7, card.exceptions); renderAny(q7, card.c_exits); renderAny(q7, card.c_exit_report); renderAny(q7, card.resilience); renderAny(q7, card.risks);
    var q8 = section(out, "8. なぜ現在の実装になっているのか", "変更履歴・TODO。理由は推測しません");
    renderAny(q8, card.history);
    out.appendChild(el("p", card.note, "hint"));
  }
  function loadCard(symbol) {
    cardSymbol = symbol;
    $("card-out").textContent = "読み込み中…";
    api("/api/card", { symbol: symbol }).then(renderCard).catch(function (e) {
      var detail = e.body && e.body.candidates ? "（候補: " + e.body.candidates.slice(0, 5).map(function (c) { return c.qualified_name; }).join(", ") + "）" : "";
      $("card-out").textContent = e.message + detail;
    });
  }
  function openCard(symbol) {
    showView("graph"); $("q").value = symbol; $("kind").value = "call"; draw();  // 呼び出しグラフを、その関数を起点に描く
    tab("card"); loadCard(symbol);
  }
  $("c-run").onclick = function () {
    var root = selected && ["function", "method", "class"].indexOf(selected.kind) >= 0 ? selected.label : $("q").value.trim();
    if (!root) { $("card-out").textContent = "グラフのノードを選ぶか、シンボルを入力してください。"; return; }
    loadCard(root);
  };

  // ---- 未解決の受信箱 / 探索 / 解析の状態 ----
  function table(headers, rows, cls) {
    var t = el("table", null, cls || "simple-table"), head = el("tr");
    headers.forEach(function (h) { head.appendChild(el("th", h)); });
    var thead = el("thead"); thead.appendChild(head); t.appendChild(thead);
    var body = el("tbody"); rows.forEach(function (r) { body.appendChild(r); }); t.appendChild(body); return t;
  }
  function cell(content, link) {
    var td = el("td");
    if (content && content.nodeType) td.appendChild(content); else if (link) { appendLinked(td, String(content)); } else td.textContent = content === undefined || content === null ? "" : String(content);
    return td;
  }
  function pathLink(path, line) {
    var a = el("a", path + ":" + line, "srclink"); a.href = "#"; a.setAttribute("role", "button");
    a.onclick = function (ev) { ev.preventDefault(); showView("graph"); showSourceAt(path, line, line); }; return a;
  }
  function viewBody(title) {
    var body = $("docs-body"); body.textContent = ""; body.classList.add("wide"); body.scrollTop = 0;
    body.appendChild(el("h2", title)); return body;
  }
  var inboxFilter = { status: "all", q: "" };
  function showUnresolved() {
    var nav = $("docs-nav"); nav.textContent = "";
    nav.appendChild(el("h3", "状態"));
    [["all", "すべて"], ["unresolved", "未解決"], ["ambiguous", "曖昧（候補が複数）"]].forEach(function (s) {
      var b = el("button", s[1]); b.type = "button"; if (inboxFilter.status === s[0]) b.className = "active";
      b.onclick = function () { inboxFilter.status = s[0]; showUnresolved(); }; nav.appendChild(b);
    });
    nav.appendChild(el("h3", "検索"));
    var input = el("input"); input.type = "search"; input.placeholder = "関数名・呼び出し先"; input.value = inboxFilter.q; input.setAttribute("aria-label", "未解決の検索");
    input.onkeydown = function (ev) { if (ev.key === "Enter") { inboxFilter.q = input.value.trim(); showUnresolved(); } }; nav.appendChild(input);
    var body = viewBody("未解決の受信箱"); body.textContent += "";
    body.appendChild(el("p", "解析器が確定できなかった呼び出しを、関数ごとに、多い順に示します。AIの推測は解析結果ではなく、別枠で扱います（実行は、操作モード、または codeinsight infer）。", "hint"));
    api("/api/unresolved", { status: inboxFilter.status, q: inboxFilter.q, limit: 100 }).then(function (data) {
      body.appendChild(el("p", data.total_functions + " 関数 / " + data.total_calls + " 呼び出し"));
      var rows = [];
      data.functions.forEach(function (f) {
        var tr = el("tr", null, "expandable"); tr.setAttribute("tabindex", "0");
        var fn = el("a", f.function, "srclink"); fn.href = "#"; fn.onclick = function (ev) { ev.preventDefault(); ev.stopPropagation(); openCard(f.function); };
        tr.appendChild(cell(fn)); tr.appendChild(cell(pathLink(f.path, f.line))); tr.appendChild(cell(f.unresolved)); tr.appendChild(cell(f.ambiguous));
        var ai = el("td");
        if (f.inference) {
          ai.appendChild(badge("ai")); ai.appendChild(badge(f.inference.status)); if (f.inference.stale) ai.appendChild(el("span", "古い", "badge b-unverified"));
          var read = el("a", "読む", "srclink"); read.href = "#"; read.onclick = function (ev) { ev.preventDefault(); ev.stopPropagation(); showInference(f.inference.id); }; ai.appendChild(read);
        } else ai.textContent = "—";
        tr.appendChild(ai); rows.push(tr);
        var detail = el("tr"); detail.hidden = true; var td = el("td"); td.colSpan = 5; td.className = "sub";
        f.calls.forEach(function (c) { var line = el("div"); line.appendChild(badge(c.status)); line.appendChild(pathLink(f.path, c.line)); line.appendChild(document.createTextNode("  " + c.name + "  ")); appendLinked(line, c.note || ""); td.appendChild(line); });
        detail.appendChild(td); rows.push(detail);
        var toggle = function () { detail.hidden = !detail.hidden; };
        tr.onclick = toggle; tr.onkeydown = function (ev) { if (ev.key === "Enter") toggle(); };
      });
      body.appendChild(table(["関数", "位置", "未解決", "曖昧", "AIの推測（保存済み）"], rows));
      if (data.total_functions > data.functions.length) body.appendChild(el("p", "…ほか " + (data.total_functions - data.functions.length) + " 関数（検索・絞り込みで探せます）", "hint"));
    }).catch(function (e) { body.appendChild(el("p", e.message)); });
  }
  function showInference(id) {
    var body = viewBody("AIの推測");
    api("/api/inference", { id: id }).then(function (d) {
      var banner = el("div", null, "ai-banner"); banner.appendChild(badge("ai")); banner.appendChild(document.createTextNode(" " + d.note)); body.appendChild(banner);
      var meta = el("p"); meta.appendChild(document.createTextNode(d.function + "  / モデル " + d.model + "  / " + d.created_at + "  ")); meta.appendChild(badge(d.status));
      if (d.stale) meta.appendChild(el("span", "古い推測（根拠のファイルが変更されています）", "badge b-unverified")); body.appendChild(meta);
      var holder = el("div"); renderMarkdown(holder, d.text, "");
      if (d.status === "unverified") { var det = el("details"); det.appendChild(el("summary", "未検証の内容を表示（事実として扱わないでください）")); det.appendChild(holder); body.appendChild(det); } else body.appendChild(holder);
      body.appendChild(el("h2", "検証の内容")); var v = el("div"); renderAny(v, d.validation); body.appendChild(v);
      var back = el("button", "未解決の受信箱へ戻る"); back.type = "button"; back.onclick = showUnresolved; body.appendChild(back);
    }).catch(function (e) { body.appendChild(el("p", e.message)); });
  }
  function showExplore(which) {
    which = which || "fields";
    var nav = $("docs-nav"); nav.textContent = ""; nav.appendChild(el("h3", "探索の軸"));
    [["fields", "フィールド（C/C++）"], ["config", "設定値"], ["boundaries", "入口と境界"]].forEach(function (s) {
      var b = el("button", s[1]); b.type = "button"; if (which === s[0]) b.className = "active"; b.onclick = function () { showExplore(s[0]); }; nav.appendChild(b);
    });
    if (which === "fields") exploreFields(); else if (which === "config") exploreList("/api/config", "設定値", "kind", "name"); else exploreList("/api/boundaries", "入口と境界", "kind", "label");
  }
  var FIELD_MODES = { read: "読み取り", write: "書き込み", readwrite: "読み書き", address: "アドレス取得", init: "初期化" };
  function exploreFields() {
    var body = viewBody("フィールドの読み書き（C/C++。プロジェクト全体）");
    var filters = el("div", null, "filters"), input = el("input"); input.type = "search"; input.placeholder = "構造体.フィールド（例: Account.balance）"; input.size = 36; input.value = HASH.field || "";
    var go = el("button", "調べる"); go.type = "button"; filters.appendChild(input); filters.appendChild(go); body.appendChild(filters);
    var out = el("div"); body.appendChild(out);
    var run = function () {
      var name = input.value.trim(); if (!name) { out.textContent = "フィールド名（または 構造体.フィールド）を入力してください。"; return; }
      out.textContent = "調べています…（名前が現れるファイルだけを構文解析します）";
      api("/api/fields", { name: name }).then(function (d) {
        out.textContent = "";
        var writers = d.uses.filter(function (u) { return u.mode !== "read"; });
        out.appendChild(el("p", "構造体・クラス: " + (d.owners.join(", ") || "—") + " / 書き込み系 " + writers.length + "件 / 読み取り " + (d.uses.length - writers.length) + "件 / 調べたファイル " + d.scanned_files + "件"));
        var rows = d.uses.map(function (u) { var tr = el("tr"); tr.appendChild(cell(pathLink(u.path, u.line))); tr.appendChild(cell(u.function || "（関数の外）")); tr.appendChild(cell(FIELD_MODES[u.mode] || u.mode)); tr.appendChild(cell(u.owner)); tr.appendChild(cell(u.text)); return tr; });
        out.appendChild(table(["位置", "関数", "種類", "構造体", "式"], rows));
        out.appendChild(el("p", d.note, "hint"));
        if (d.skipped.length) out.appendChild(el("p", "調べられなかったファイル: " + d.skipped.join(", "), "hint"));
      }).catch(function (e) { out.textContent = e.message; });
    };
    go.onclick = run; input.onkeydown = function (ev) { if (ev.key === "Enter") run(); };
    if (input.value) run();
  }
  function exploreList(path, title, kindKey, nameKey) {
    var body = viewBody(title); var filters = el("div", null, "filters"), input = el("input"); input.type = "search"; input.placeholder = "名前で絞り込み"; filters.appendChild(input);
    var go = el("button", "絞り込む"); go.type = "button"; filters.appendChild(go); body.appendChild(filters);
    var out = el("div"); out.textContent = "読み込み中…（大きなプロジェクトでは、初回に数十秒かかります）"; body.appendChild(out);
    var run = function () {
      api(path, { q: input.value.trim() }).then(function (d) {
        out.textContent = ""; out.appendChild(el("p", d.total + " 件" + (d.items.length < d.total ? "（先頭 " + d.items.length + " 件を表示）" : "")));
        var rows = d.items.map(function (i) {
          var tr = el("tr"); tr.appendChild(cell(d.kinds[i[kindKey]] || i[kindKey])); tr.appendChild(cell(i[nameKey])); tr.appendChild(cell(pathLink(i.path, i.line)));
          tr.appendChild(cell(i.owner || "")); var extra = el("td"); if (i.confidence && i.confidence !== "confirmed") extra.appendChild(badge("inferred"));
          extra.appendChild(document.createTextNode([i.default ? "既定値: " + i.default : "", i.detail || "", i.target ? "→ " + i.target : ""].filter(Boolean).join(" / "))); tr.appendChild(extra); return tr;
        });
        out.appendChild(table(["種類", "名前", "位置", "含む関数", "備考"], rows));
        if (d.skipped && d.skipped.length) out.appendChild(el("p", "調べられなかったファイル: " + d.skipped.join(", "), "hint"));
      }).catch(function (e) { out.textContent = e.message; });
    };
    go.onclick = run; input.onkeydown = function (ev) { if (ev.key === "Enter") run(); }; run();
  }
  function showStatus() {
    var nav = $("docs-nav"); nav.textContent = ""; nav.appendChild(el("h3", "解析の状態"));
    var body = viewBody("解析の状態");
    api("/api/status", {}).then(function (d) {
      if (!d.latest) body.appendChild(el("p", "解析の記録がありません。")); else {
        var l = d.latest, p = el("p"); p.appendChild(document.createTextNode("前回の解析: " + l.timestamp + " / 解析器 " + l.analyzer_version + " / リビジョン " + (l.repository_revision || "—") + " / "));
        p.appendChild(el("span", l.status === "success" ? "✓ 成功" : l.status === "partial" ? "△ 一部失敗" : "✗ 失敗", "badge " + (l.status === "success" ? "b-verified" : l.status === "partial" ? "b-partial" : "b-unverified")));
        p.appendChild(document.createTextNode("  警告 " + l.warning_count + "件 / エラー " + l.error_count + "件")); body.appendChild(p);
      }
      body.appendChild(el("h2", "言語ごとのファイル"));
      var rows = Object.keys(d.files_by_language).map(function (lang) { var s = d.files_by_language[lang], tr = el("tr"); tr.appendChild(cell(lang)); tr.appendChild(cell(s.analyzed || 0)); tr.appendChild(cell(s.failed || 0)); return tr; });
      body.appendChild(table(["言語", "解析できた", "失敗（正常として扱っていない）"], rows));
      body.appendChild(el("h2", "解析後に変更されたファイル（" + d.stale_count + "件）"));
      if (d.stale_files.length) { var ul = el("ul"); d.stale_files.slice(0, 50).forEach(function (f) { ul.appendChild(el("li", f)); }); body.appendChild(ul); body.appendChild(el("p", "これらのファイルの位置は、解析時と対応しない可能性があります。再解析してください。", "hint")); } else body.appendChild(el("p", "なし"));
      body.appendChild(el("h2", "失敗の原因（多い順）"));
      if (!d.error_groups.length) body.appendChild(el("p", "なし"));
      d.error_groups.forEach(function (g) {
        var box = el("div"); box.appendChild(el("strong", g.count + "件  ")); box.appendChild(document.createTextNode(g.cause));
        if (g.hint) box.appendChild(el("div", g.hint, "hint")); var ex = el("ul"); g.examples.forEach(function (e) { ex.appendChild(el("li", e, "hint")); }); box.appendChild(ex); body.appendChild(box);
      });
      body.appendChild(el("p", d.note, "hint"));
    }).catch(function (e) { body.appendChild(el("p", e.message)); });
  }

  // 上部の設定（種類・方向・深さ・シンボル）を変えたら、すぐに描き直す。「描画」ボタンは、同じ設定での再描画に使う。
  $("draw").onclick = draw;
  $("q").onkeydown = function (ev) { if (ev.key === "Enter") draw(); };
  $("q").onchange = draw;  // 候補（datalist）の選択・入力の確定
  $("kind").onchange = draw;
  $("direction").onchange = draw;
  $("depth").onchange = draw;
  var drawTimer = null;
  $("depth").oninput = function () { clearTimeout(drawTimer); drawTimer = setTimeout(draw, 400); };  // 数値の連続入力は、まとめて1回
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
    $("pane-source").hidden = name !== "source"; $("pane-extract").hidden = name !== "extract"; $("pane-card").hidden = name !== "card";
    $("tab-source").classList.toggle("active", name === "source"); $("tab-extract").classList.toggle("active", name === "extract"); $("tab-card").classList.toggle("active", name === "card");
  }
  $("tab-source").onclick = function () { tab("source"); };
  $("tab-extract").onclick = function () { tab("extract"); };
  $("tab-card").onclick = function () { tab("card"); if (!cardSymbol && selected && ["function", "method"].indexOf(selected.kind) >= 0) loadCard(selected.label); };
  $("x-run").onclick = function () {
    var root = selected && selected.kind !== "block" && selected.kind !== "decision" && selected.kind !== "terminal" ? selected.label : $("q").value.trim();
    if (!root) { $("x-out").textContent = "グラフのノードを選ぶか、シンボルを入力してください。"; return; }
    $("x-out").textContent = "読み込み中…";
    api("/api/extract", { root: root, direction: $("x-direction").value, depth: $("x-depth").value }).then(function (data) {
      lastMarkdown = data.markdown; renderMarkdown($("x-out"), data.markdown, ""); $("x-save").disabled = false;
    }).catch(function (e) { $("x-out").textContent = e.message; $("x-save").disabled = true; });
  };
  $("x-save").onclick = function () {
    var blob = new Blob([lastMarkdown], { type: "text/markdown;charset=utf-8" });
    var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "extract.md"; a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  };

  // 操作説明用: 要素に番号の印を付ける（#annotate=controls|panel）
  var GUIDE = {   // 集合 -> [番号の印の基準にする容器, 印を付ける要素...]
    controls: ["controls", "view-graph", "q", "kind", "direction", "depth", "draw"],
    panel: ["reader", "tab-source", "tab-extract", "reader-title", "splitter", "source"],
    docs: ["docs-view", "docs-nav", "docs-body"]
  };
  function annotate(set) {
    [].forEach.call(document.querySelectorAll(".guide-badge"), function (b) { b.remove(); });  // 配置が変わったあとにも、付け直せるようにする
    var ids = GUIDE[set] || [], container = ids.length ? $(ids[0]) : null;
    if (!container) return;
    container.style.position = "relative";   // 印は、容器の中の位置で置く（画面の大きさが変わっても、要素からずれない）
    var base = container.getBoundingClientRect(), number = 0;
    ids.slice(1).forEach(function (id) {
      var target = $(id); if (!target || target.offsetParent === null) return;
      var box = target.getBoundingClientRect(), badge = document.createElement("span");
      badge.textContent = String(++number); badge.className = "guide-badge";
      badge.setAttribute("style", "position:absolute;z-index:50;left:" + Math.max(0, box.left - base.left - 6) + "px;top:" + (box.top - base.top - 8) + "px;width:20px;height:20px;line-height:20px;text-align:center;border-radius:50%;background:#dc2626;color:#fff;font:700 12px system-ui;box-shadow:0 0 0 2px #fff");
      container.appendChild(badge);
    });
  }
  function settle(fn) { setTimeout(fn, 1200); setTimeout(fn, 3500); }

  api("/api/project", {}).then(function (info) {
    $("project-name").textContent = info.name;
    $("project-stats").textContent = "  ファイル " + info.files + " / シンボル " + info.symbols + (info.stale_count ? " / 解析後に変更されたファイル " + info.stale_count + "件" : "");
    ["kind", "direction", "depth"].forEach(function (k) { if (HASH[k]) $(k).value = HASH[k]; });
    if (HASH.root) $("q").value = HASH.root;
    if (!HASH.kind && !HASH.root) $("kind").value = "arch";
    var drawn = draw();
    if (["reading", "help", "unresolved", "explore", "status"].indexOf(HASH.view) >= 0) {
      Promise.resolve(drawn).then(function () { return showView(HASH.view); }).then(function () { settle(function () { if (HASH.annotate) annotate(HASH.annotate); }); });
      return;
    }
    Promise.resolve(drawn).then(function () {
      if (HASH.select) {
        var node = [].filter.call(document.querySelectorAll(".node"), function (n) { return n.textContent.indexOf(HASH.select) >= 0; })[0];
        if (node) node.dispatchEvent(new Event("click"));
      }
      if (HASH.tab === "extract") { tab("extract"); $("x-run").click(); }
      settle(function () { if (HASH.annotate) annotate(HASH.annotate); });
    });
    if (HASH.view === undefined && !HASH.kind && !HASH.root) {
      // 資料（make reading の成果物）があれば、最初に資料の目次を開く。グラフ・ソース・切り出しは、そこから開ける
      api("/api/reading", {}).then(function (data) { if (data.available && data.files.length) showView("reading"); }).catch(function () {});
    }
  }).catch(function (e) { say(e.message); });
})();
"""


def content_security_policy(nonce: str, header: bool = True) -> str:
    """ページのCSP。HTTPヘッダーでは frame-ancestors も付ける（<meta> では無効のため、metaには付けない）。"""

    policy = f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; img-src data: blob:; base-uri 'none'; form-action 'none'"
    return policy + ("; frame-ancestors 'none'" if header else "")


def render_app(token: str, nonce: str) -> str:
    csp = content_security_policy(nonce, header=False)
    payload = json.dumps(token).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return render_template(csp, _CONTROLS, _READER, nonce, _BOOT.replace("__TOKEN__", payload))
