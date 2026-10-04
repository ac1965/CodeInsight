from __future__ import annotations

import json

from codeinsight.application.graph_builder import GraphModel
from codeinsight.presentation.graph_export import model_to_dict

# 外部リソースを一切読み込まない、自己完結型のビューアー。
# ラベルやパスは解析対象（信頼できない入力）由来のため、DOMへは textContent / setAttribute
# のみで反映し、innerHTML は使わない。
_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="__CSP__">
<title>CodeInsight グラフビューアー</title>
<style>
:root { color-scheme: light dark; --bg:#ffffff; --fg:#1f2937; --muted:#6b7280; --line:#9ca3af;
  --node:#eff6ff; --node-stroke:#2563eb; --warn:#b45309; --warn-bg:#fef3c7; --ext:#6b7280; --ext-bg:#f3f4f6;
  --panel:#f9fafb; --border:#e5e7eb; --hit:#fde68a; }
@media (prefers-color-scheme: dark) { :root { --bg:#111827; --fg:#e5e7eb; --muted:#9ca3af; --line:#6b7280;
  --node:#1e3a8a; --node-stroke:#60a5fa; --warn:#fbbf24; --warn-bg:#451a03; --ext:#9ca3af; --ext-bg:#1f2937;
  --panel:#1f2937; --border:#374151; --hit:#854d0e; } }
body { margin:0; font:14px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
header { padding:10px 16px; border-bottom:1px solid var(--border); }
h1 { font-size:16px; margin:0 0 4px; }
.notes { color:var(--muted); font-size:12px; margin:0; }
main { display:flex; height:calc(100vh - 92px); min-height:320px; }
#canvas { flex:1; overflow:auto; }
aside { width:300px; border-left:1px solid var(--border); background:var(--panel); padding:12px; overflow:auto; }
.toolbar { padding:8px 16px; border-bottom:1px solid var(--border); display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
input[type=search] { padding:4px 8px; border:1px solid var(--border); border-radius:4px; background:var(--bg); color:var(--fg); }
.legend span { display:inline-flex; align-items:center; gap:4px; margin-right:10px; font-size:12px; color:var(--muted); }
.legend svg { width:28px; height:8px; }
.node rect { fill:var(--node); stroke:var(--node-stroke); }
.node.unresolved rect { fill:var(--warn-bg); stroke:var(--warn); stroke-dasharray:4 3; }
.node.external rect { fill:var(--ext-bg); stroke:var(--ext); stroke-dasharray:4 3; }
.node.hit rect { fill:var(--hit); }
.node.selected rect { stroke-width:3; }
.node.faded { opacity:0.25; }
button { padding:3px 10px; border:1px solid var(--border); border-radius:4px; background:var(--bg); color:var(--fg); cursor:pointer; }
button:hover { background:var(--panel); }
.node text { fill:var(--fg); font-size:12px; pointer-events:none; }
.node { cursor:pointer; }
.edge { fill:none; stroke:var(--line); stroke-width:1.4; }
.edge.inferred { stroke-dasharray:6 4; }
.edge.unresolved { stroke:var(--warn); stroke-dasharray:2 4; }
.edge.external { stroke:var(--ext); stroke-dasharray:2 4; }
.edgeg.dim { opacity:0.12; }
.edge-label { fill:var(--muted); font-size:10px; text-anchor:middle; pointer-events:none; }
.node.root rect { fill:var(--hit); stroke:var(--warn); stroke-width:3; }
.node.root text { font-weight:700; }
.col-title { fill:var(--muted); font-size:12px; font-weight:600; }
.group-title { fill:var(--muted); font-size:10px; }
.group-line { stroke:var(--border); stroke-width:1; }
.neighbors { margin:4px 0 0; padding-left:16px; font-size:12px; }
body.app main { height:calc(100vh - 380px); min-height:280px; }
#reader { border-top:1px solid var(--border); padding:8px 16px; }
#reader .tabs button.active { background:var(--hit); }
#reader pre { margin:6px 0; max-height:40vh; overflow:auto; padding:8px; background:var(--panel); border:1px solid var(--border); border-radius:4px; font:12px/1.45 ui-monospace, monospace; white-space:pre; }
.src-line.hl { background:var(--hit); }
.src-line .n { display:inline-block; min-width:5ch; text-align:right; color:var(--muted); margin-right:1ch; }
#controls { padding:8px 16px; border-bottom:1px solid var(--border); display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
#controls select, #controls input { padding:3px 6px; border:1px solid var(--border); border-radius:4px; background:var(--bg); color:var(--fg); }
.node.decision rect { fill:var(--warn-bg); stroke:var(--node-stroke); }
.node.terminal rect { fill:var(--ext-bg); stroke:var(--ext); rx:14; }
dt { color:var(--muted); font-size:12px; margin-top:8px; }
dd { margin:0; word-break:break-all; white-space:pre-wrap; }
@media (max-width: 640px) { main { flex-direction:column; height:auto; } aside { width:auto; border-left:none; border-top:1px solid var(--border); } #canvas { height:60vh; } }
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <p class="notes" id="notes"></p>
</header>
__CONTROLS__
<div class="toolbar">
  <input id="filter" type="search" placeholder="ノードを検索" aria-label="ノードを検索">
  <span>
    <button id="zoom-out" type="button" aria-label="縮小">−</button>
    <button id="zoom-in" type="button" aria-label="拡大">＋</button>
    <button id="zoom-fit" type="button">全体表示</button>
  </span>
  <span class="legend" id="legend"></span>
  <span id="stats" class="notes"></span>
</div>
<main>
  <div id="canvas"><svg id="graph" role="img" aria-label="グラフ"></svg></div>
  <aside id="detail"><p class="notes">ノードまたは辺をクリックすると詳細を表示します。</p></aside>
</main>
__READER__
__DATA_SCRIPT__
<script__NONCE__>
function renderGraph(data, hooks) {
  "use strict";
  hooks = hooks || {};
  var NS = "http://www.w3.org/2000/svg";
  var NODE_W = 230, NODE_H = 30, GAP_X = 90, GAP_Y = 18, PAD = 24;

  document.getElementById("title").textContent = data.title;
  document.title = "CodeInsight グラフビューアー: " + data.title;
  document.getElementById("notes").textContent = data.notes.join(" ");
  document.getElementById("stats").textContent = "ノード " + data.nodes.length + " / 辺 " + data.edges.length;

  var legend = document.getElementById("legend");
  legend.textContent = "";
  document.getElementById("graph").textContent = "";
  [["confirmed", "確定"], ["inferred", "推定"], ["unresolved", "未解決"], ["external", "外部"]].forEach(function (item) {
    var span = document.createElement("span");
    var svg = document.createElementNS(NS, "svg");
    var line = document.createElementNS(NS, "line");
    line.setAttribute("x1", "0"); line.setAttribute("x2", "28"); line.setAttribute("y1", "4"); line.setAttribute("y2", "4");
    line.setAttribute("class", "edge " + item[0]);
    svg.appendChild(line); span.appendChild(svg);
    span.appendChild(document.createTextNode(item[1]));
    legend.appendChild(span);
  });

  var ids = {};
  data.nodes.forEach(function (n, i) { ids[n.id] = i; });
  var edges = data.edges.filter(function (e) { return e.source in ids && e.target in ids; });

  // 層の割り当て: 深さ優先で逆向きの辺(閉路)を除き、最長経路で層を決める。
  var out = data.nodes.map(function () { return []; });
  edges.forEach(function (e) { out[ids[e.source]].push(ids[e.target]); });
  var state = data.nodes.map(function () { return 0; });
  var dag = data.nodes.map(function () { return []; });
  var indegree = data.nodes.map(function () { return 0; });
  data.nodes.forEach(function (_, start) {
    if (state[start]) return;
    var stack = [[start, 0]]; state[start] = 1;
    while (stack.length) {
      var top = stack[stack.length - 1];
      if (top[1] < out[top[0]].length) {
        var next = out[top[0]][top[1]++];
        if (state[next] === 0) { state[next] = 1; dag[top[0]].push(next); indegree[next]++; stack.push([next, 0]); }
        else if (state[next] === 2) { dag[top[0]].push(next); indegree[next]++; }
      } else { state[top[0]] = 2; stack.pop(); }
    }
  });
  var rank = data.nodes.map(function () { return 0; });
  var queue = [];
  indegree.forEach(function (d, i) { if (d === 0) queue.push(i); });
  while (queue.length) {
    var current = queue.shift();
    dag[current].forEach(function (next) {
      rank[next] = Math.max(rank[next], rank[current] + 1);
      if (--indegree[next] === 0) queue.push(next);
    });
  }
  var columns = {};
  data.nodes.forEach(function (n, i) { (columns[rank[i]] = columns[rank[i]] || []).push(i); });
  var pos = [], maxX = 0, maxY = 0;
  // 制御フロー図は上から下へ、それ以外は左から右へ層を並べる。
  var vertical = data.graph_kind === "flow";
  var focusNode = data.focus !== null && data.focus !== undefined && data.focus in ids;
  var headers = [], groupTitles = [];
  if (focusNode) {
    // 起点を中心に、起点に依存している側（Depended On By）を左、起点が依存している側（Depends On）を右へ、距離ごとの列に並べる。
    // 同じ列の中では、ファイル（ディレクトリ）ごとにまとめる。起点に結び付かないノードは、最後の列に置く。
    var HEAD = 40, GROUP_GAP = 26;
    var dist = data.nodes.map(function (n) { return typeof n.distance === "number" ? n.distance : null; });
    var lo = 0, hi = 0;
    dist.forEach(function (d) { if (d !== null) { lo = Math.min(lo, d); hi = Math.max(hi, d); } });
    var byColumn = {};
    dist.forEach(function (d, i) { var c = d === null ? hi + 1 : d; (byColumn[c] = byColumn[c] || []).push(i); });
    Object.keys(byColumn).forEach(function (c) {
      var members = byColumn[c].slice().sort(function (a, b) {
        var ga = data.nodes[a].group || "", gb = data.nodes[b].group || "";
        return ga < gb ? -1 : ga > gb ? 1 : (data.nodes[a].label < data.nodes[b].label ? -1 : 1);
      });
      var x = PAD + (Number(c) - lo) * (NODE_W + GAP_X), y = PAD + HEAD, last = null;
      members.forEach(function (i) {
        var group = data.nodes[i].group || "";
        if (group !== last) {
          if (last !== null) y += GROUP_GAP - GAP_Y;
          groupTitles.push({ x: x, y: y + 2, text: group || "(その他)" });
          y += 14; last = group;
        }
        pos[i] = { x: x, y: y };
        y += NODE_H + GAP_Y;
        maxX = Math.max(maxX, x + NODE_W + 50); maxY = Math.max(maxY, y);
      });
      var title = Number(c) < 0 ? "Depended On By（" + (-c) + "段階前）" : Number(c) === 0 ? "起点" :
        Number(c) > hi ? "起点に結び付かないもの" : "Depends On（" + c + "段階先）";
      headers.push({ x: x, y: PAD + 12, text: title });
    });
  } else {
    Object.keys(columns).forEach(function (r) {
      columns[r].forEach(function (i, row) {
        pos[i] = vertical
          ? { x: PAD + row * (NODE_W + 40), y: PAD + r * (NODE_H + 46) }
          : { x: PAD + r * (NODE_W + GAP_X), y: PAD + row * (NODE_H + GAP_Y) };
        maxX = Math.max(maxX, pos[i].x + NODE_W + 50); maxY = Math.max(maxY, pos[i].y + NODE_H);
      });
    });
  }

  var svg = document.getElementById("graph");
  var contentW = maxX + PAD, contentH = maxY + PAD, scale = 1;
  var canvas = document.getElementById("canvas");
  svg.setAttribute("viewBox", "0 0 " + contentW + " " + contentH);
  function applyScale(value) {
    scale = Math.min(3, Math.max(0.05, value));
    svg.setAttribute("width", Math.round(contentW * scale));
    svg.setAttribute("height", Math.round(contentH * scale));
  }
  function fitScale() { return Math.min(1, (canvas.clientWidth - 8) / contentW); }
  function fit() { applyScale(fitScale()); }
  document.getElementById("zoom-in").onclick = function () { applyScale(scale * 1.25); };
  document.getElementById("zoom-out").onclick = function () { applyScale(scale / 1.25); };
  document.getElementById("zoom-fit").onclick = fit;
  canvas.onwheel = function (ev) {
    if (!(ev.ctrlKey || ev.metaKey)) return;
    ev.preventDefault();
    applyScale(scale * (ev.deltaY < 0 ? 1.1 : 1 / 1.1));
  };
  applyScale(Math.max(0.7, fitScale()));  // 初期表示は文字が読める倍率にし、全体表示はボタンで
  var defs = document.createElementNS(NS, "defs");
  var marker = document.createElementNS(NS, "marker");
  marker.setAttribute("id", "arrow"); marker.setAttribute("viewBox", "0 0 10 10"); marker.setAttribute("refX", "9");
  marker.setAttribute("refY", "5"); marker.setAttribute("markerWidth", "7"); marker.setAttribute("markerHeight", "7");
  marker.setAttribute("orient", "auto-start-reverse");
  var arrow = document.createElementNS(NS, "path");
  arrow.setAttribute("d", "M0,0 L10,5 L0,10 z"); arrow.setAttribute("fill", "#9ca3af");
  marker.appendChild(arrow); defs.appendChild(marker); svg.appendChild(defs);

  headers.forEach(function (h) {
    var t = document.createElementNS(NS, "text");
    t.setAttribute("class", "col-title"); t.setAttribute("x", h.x); t.setAttribute("y", h.y); t.textContent = h.text;
    svg.appendChild(t);
  });
  groupTitles.forEach(function (g) {
    var t = document.createElementNS(NS, "text");
    t.setAttribute("class", "group-title"); t.setAttribute("x", g.x); t.setAttribute("y", g.y + 8);
    t.textContent = g.text.length > 44 ? "…" + g.text.slice(-43) : g.text;
    var title = document.createElementNS(NS, "title"); title.textContent = g.text; t.appendChild(title);
    svg.appendChild(t);
  });

  var detail = document.getElementById("detail");
  function row(dl, term, value) {
    if (value === null || value === undefined || value === "") return;
    var dt = document.createElement("dt"); dt.textContent = term;
    var dd = document.createElement("dd"); dd.textContent = String(value);
    dl.appendChild(dt); dl.appendChild(dd);
  }
  function show(title, rows) {
    detail.textContent = "";
    var h = document.createElement("h2"); h.style.fontSize = "14px"; h.textContent = title; detail.appendChild(h);
    var dl = document.createElement("dl");
    rows.forEach(function (r) { row(dl, r[0], r[1]); });
    detail.appendChild(dl);
  }
  var STYLE_LABEL = { confirmed: "確定", inferred: "推定", unresolved: "未解決", external: "外部" };

  var edgeEls = [];
  edges.forEach(function (e) {
    var a = pos[ids[e.source]], b = pos[ids[e.target]];
    var x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
    if (vertical) {
      x1 = a.x + NODE_W / 2; y1 = a.y + NODE_H; x2 = b.x + NODE_W / 2; y2 = b.y;
      if (b.y <= a.y) { y1 = a.y; y2 = b.y + NODE_H; }  // 戻る辺（ループ）は上から出て下に入る
    } else if (b.x <= a.x) { x1 = a.x; x2 = b.x + NODE_W; }
    var mid = (x1 + x2) / 2, midY = (y1 + y2) / 2;
    var path = document.createElementNS(NS, "path");
    if (e.source === e.target) {
      // 自己再帰: ノードの右側に小さなループを描く
      var rx = a.x + NODE_W, ry = a.y;
      path.setAttribute("d", "M" + rx + "," + (ry + 8) + " C" + (rx + 42) + "," + (ry - 10) + " " + (rx + 42) + "," + (ry + NODE_H + 10) + " " + rx + "," + (ry + NODE_H - 8));
    } else if (vertical) {
      path.setAttribute("d", "M" + x1 + "," + y1 + " C" + x1 + "," + midY + " " + x2 + "," + midY + " " + x2 + "," + y2);
    } else {
      path.setAttribute("d", "M" + x1 + "," + y1 + " C" + mid + "," + y1 + " " + mid + "," + y2 + " " + x2 + "," + y2);
    }
    path.setAttribute("class", "edge " + e.style);
    path.setAttribute("marker-end", "url(#arrow)");
    path.style.cursor = "pointer";
    var group = document.createElementNS(NS, "g");
    group.setAttribute("class", "edgeg");
    path.addEventListener("click", function () {
      show("辺: " + e.kind, [
        ["確からしさ", STYLE_LABEL[e.style] || e.style],
        ["呼び出し元/依存元", data.nodes[ids[e.source]].label],
        ["呼び出し先/依存先", data.nodes[ids[e.target]].label],
        ["ラベル", e.label],
        ["回数", e.count],
        ["根拠位置", e.evidence.join("\\n")],
        ["注記", e.note]
      ]);
    });
    group.appendChild(path);
    if (e.label) {
      var caption = document.createElementNS(NS, "text");
      caption.setAttribute("class", "edge-label");
      caption.setAttribute("x", (x1 + x2) / 2);
      caption.setAttribute("y", (y1 + y2) / 2 - 3);
      if (vertical) { caption.setAttribute("y", midY); caption.setAttribute("x", (x1 + x2) / 2 + 4); caption.style.textAnchor = "start"; }
      caption.textContent = e.label;
      group.appendChild(caption);
    }
    svg.appendChild(group);
    edgeEls.push({ el: group, e: e });
  });

  var nodeEls = [];
  var selected = null;
  data.nodes.forEach(function (n, i) {
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node " + n.kind + (focusNode && n.id === data.focus ? " root" : ""));
    g.setAttribute("transform", "translate(" + pos[i].x + "," + pos[i].y + ")");
    var rect = document.createElementNS(NS, "rect");
    rect.setAttribute("width", NODE_W); rect.setAttribute("height", NODE_H); rect.setAttribute("rx", "5");
    var text = document.createElementNS(NS, "text");
    text.setAttribute("x", "8"); text.setAttribute("y", "19");
    // 修飾名は末尾（関数名側）のほうが識別しやすいので、長い場合は先頭を省略する。
    var qualified = ["function", "method", "class", "module", "file"].indexOf(n.kind) >= 0;
    var label = n.label.length <= 32 ? n.label
      : (qualified ? "…" + n.label.slice(-31) : n.label.slice(0, 31) + "…");
    text.textContent = label;
    var title = document.createElementNS(NS, "title"); title.textContent = n.label;
    g.appendChild(title); g.appendChild(rect); g.appendChild(text);
    g.addEventListener("click", function () {
      if (selected) selected.classList.remove("selected");
      selected = g; g.classList.add("selected");
      var related = edges.filter(function (e) { return e.source === n.id || e.target === n.id; });
      var near = {}; near[n.id] = true;
      related.forEach(function (e) { near[e.source] = true; near[e.target] = true; });
      edgeEls.forEach(function (x) { x.el.classList.toggle("dim", related.indexOf(x.e) < 0); });
      nodeEls.forEach(function (x) { x.el.classList.toggle("faded", !near[x.n.id]); });
      var dependsOn = edges.filter(function (e) { return e.source === n.id; }).map(function (e) { return data.nodes[ids[e.target]].label; });
      var dependedBy = edges.filter(function (e) { return e.target === n.id; }).map(function (e) { return data.nodes[ids[e.source]].label; });
      if (hooks.onNode) hooks.onNode(n);
      show(n.label, [
        ["種別", n.kind],
        ["場所", n.path ? n.path + (n.line ? ":" + n.line : "") : ""],
        ["Depends On（直接の依存先・呼び出し先）: " + dependsOn.length, dependsOn.slice(0, 40).join("\\n") + (dependsOn.length > 40 ? "\\n…" : "")],
        ["Depended On By（直接の依存元・呼び出し元）: " + dependedBy.length, dependedBy.slice(0, 40).join("\\n") + (dependedBy.length > 40 ? "\\n…" : "")]
      ]);
    });
    svg.appendChild(g);
    nodeEls.push({ el: g, n: n });
  });

  if (focusNode) {
    var rootEl = nodeEls.filter(function (x) { return x.n.id === data.focus; })[0];
    if (rootEl) {
      rootEl.el.dispatchEvent(new Event("click"));
      canvas.scrollTo(Math.max(0, pos[ids[data.focus]].x * scale - canvas.clientWidth / 3), 0);
    }
  }
  document.getElementById("filter").oninput = function (ev) {
    var q = ev.target.value.toLowerCase();
    var first = null;
    nodeEls.forEach(function (x, i) {
      var hit = q !== "" && x.n.label.toLowerCase().indexOf(q) >= 0;
      x.el.classList.toggle("hit", hit);
      if (hit && first === null) first = i;
    });
    if (first !== null) {
      canvas.scrollTo(Math.max(0, pos[first].x * scale - 40), Math.max(0, pos[first].y * scale - 40));
    }
  };
  svg.onclick = function (ev) {
    if (ev.target === svg) {
      edgeEls.forEach(function (x) { x.el.classList.remove("dim"); });
      nodeEls.forEach(function (x) { x.el.classList.remove("faded"); });
      if (selected) selected.classList.remove("selected");
      selected = null;
    }
  };
}
__BOOT__
</script>
</body>
</html>
"""


def _embed(model: GraphModel) -> str:
    """JSONをscriptタグ内へ安全に埋め込む（`</script>`等で抜け出せないようにする）。"""

    payload = json.dumps(model_to_dict(model), ensure_ascii=False)
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


_STATIC_CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:"


def render_html(model: GraphModel) -> str:
    """グラフ1つを埋め込んだ、自己完結型のHTML（外部リソースも通信も使わない）。"""

    boot = 'renderGraph(JSON.parse(document.getElementById("graph-data").textContent), {});'
    return (
        _TEMPLATE.replace("__CSP__", _STATIC_CSP).replace("__CONTROLS__", "").replace("__READER__", "").replace("__NONCE__", "")
        .replace("__DATA_SCRIPT__", f'<script id="graph-data" type="application/json">{_embed(model)}</script>')
        .replace("__BOOT__", boot)
    )


def render_template(csp: str, controls: str, reader: str, nonce: str, boot: str) -> str:
    """動的なビューアー（serve）用の骨組み。データはAPIから取得するため、埋め込まない。"""

    return (
        _TEMPLATE.replace("__CSP__", csp).replace("__CONTROLS__", controls).replace("__READER__", reader).replace("__NONCE__", f' nonce="{nonce}"')
        .replace("__DATA_SCRIPT__", "").replace("__BOOT__", boot)
    )
