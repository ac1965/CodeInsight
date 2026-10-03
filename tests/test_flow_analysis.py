from __future__ import annotations

import ast
from pathlib import Path

from codeinsight.analysis import flow_analysis as fa


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(name)


def test_control_flow_items_and_metrics(python_flow_dir: Path) -> None:
    summary = fa.analyze_control_flow(_function(python_flow_dir / "flow.py", "fetch"))
    kinds = [(i.kind, i.depth) for i in summary.items]

    assert ("for", 0) in kinds and ("try", 1) in kinds and ("break", 2) in kinds
    assert [k for k, _ in kinds].count("except") == 2
    assert {"if", "elif", "else"} <= {k for k, _ in kinds}  # if / elif / else
    assert ("raise", 1) in kinds and ("return", 1) in kinds
    metrics = summary.metrics
    assert metrics["loops"] == 1 and metrics["try_blocks"] == 1 and metrics["handlers"] == 2
    assert metrics["raises"] == 1 and metrics["returns"] == 2
    # 循環的複雑度 = 1 + for + if + elif + except×2 （BoolOp・内包表記は無い）
    assert metrics["cyclomatic"] == 1 + 1 + 1 + 1 + 2


def test_handlers_describe_swallowing_and_logging(python_flow_dir: Path) -> None:
    summary = fa.analyze_control_flow(_function(python_flow_dir / "flow.py", "fetch"))
    by_type = {h.types: h for h in summary.handlers}

    assert by_type[("OSError",)].swallowed is False  # sleep して continue（リトライ）
    assert by_type[("Exception",)].swallowed is True  # pass のみ
    assert all(not h.reraises for h in summary.handlers)


def test_resilience_hints_find_retry_timeout_and_sleep(python_flow_dir: Path) -> None:
    summary = fa.analyze_control_flow(_function(python_flow_dir / "flow.py", "fetch"))
    hints = {(h.kind, h.line) for h in summary.hints}
    kinds = {k for k, _ in hints}

    assert kinds == {"retry", "timeout", "sleep"}
    timeout = next(h for h in summary.hints if h.kind == "timeout")
    assert "timeout=10" in timeout.detail


def test_raises_record_enclosing_handlers(python_flow_dir: Path) -> None:
    summary = fa.analyze_control_flow(_function(python_flow_dir / "flow.py", "fetch"))
    (raised,) = summary.raises
    assert raised.exception == "NotFound" and raised.handlers == ()  # tryの外で、捕捉されない

    caller = fa.analyze_control_flow(_function(python_flow_dir / "flow.py", "caller"))
    (try_range,) = caller.tries
    assert try_range.handler_types == (("NotFound",),)


def test_exception_caught_uses_builtin_and_project_hierarchies() -> None:
    assert fa.exception_caught("FileNotFoundError", ("OSError",))  # 組み込みの継承関係
    assert fa.exception_caught("ValueError", ("Exception",)) and fa.exception_caught("X", ("<bare>",))
    assert not fa.exception_caught("ValueError", ("KeyError",))

    bases = {"NotFound": {"RetryError", "Exception"}}
    assert fa.exception_caught("NotFound", ("RetryError",), lambda n: bases.get(n, set()))
    assert not fa.exception_caught("NotFound", ("RetryError",))  # 基底クラスが分からなければ捕捉とみなさない


def test_variable_lifecycle_and_propagation(python_flow_dir: Path) -> None:
    variables = fa.analyze_variables(_function(python_flow_dir / "flow.py", "fetch"))

    url = variables["url"]
    assert url.is_param
    (flow,) = [f for f in url.flows if f.kind == "call_arg"]
    assert flow.target == "requests.get" and flow.position == 0

    data = variables["data"]
    assert [d.how for d in data.definitions] == ["assign", "assign"]  # None, resp.json()
    assert data.definitions[1].depends_on == ("resp",)
    kinds = {(f.kind, f.target) for f in data.flows}
    assert ("call_arg", "transform") in kinds  # transform(data, scale=2)
    assert ("derive", "result") in kinds or ("copy", "result") in kinds

    result = variables["result"]
    assert any(f.kind == "return" for f in result.flows)
    transform_call = next(f for f in data.flows if f.kind == "call_arg" and f.target == "transform")
    assert transform_call.position == 0 and transform_call.keyword is None

    keyword_flows = [f for f in variables["retries"].flows if f.kind == "call_arg"]
    assert keyword_flows and keyword_flows[0].target == "range"


def test_class_state_separates_writes_mutations_and_reads(python_flow_dir: Path) -> None:
    tree = ast.parse((python_flow_dir / "flow.py").read_text(encoding="utf-8"))
    counter = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Counter")
    accesses = {(a.attribute, a.method, a.mode) for a in fa.analyze_class_state(counter)}

    assert ("count", "__init__", "write") in accesses
    assert ("count", "incr", "write") in accesses  # self.count += 1
    assert ("items", "incr", "mutate") in accesses  # self.items.append(...)
    assert ("count", "value", "read") in accesses


def test_module_state_finds_global_writes(python_flow_dir: Path) -> None:
    tree = ast.parse((python_flow_dir / "flow.py").read_text(encoding="utf-8"))
    writes = {(w.name, w.function, w.mode) for w in fa.analyze_module_state(tree)}
    assert ("REGISTRY", "register", "global_assign") in writes
    assert ("CACHE", "register", "mutate") in writes
