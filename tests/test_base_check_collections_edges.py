"""集合声明护栏（`base bare-collections`）的分支用例。

覆盖：注解形态矩阵（泛型 / 裸形态 / 协议 / 联合 / 字符串前向引用 / tuple 同构）、
扫描目标展开（服务分组双目录、排除目录、重复目标去重）、位置归类与原因文案兜底。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from base_fixtures import config, write

from engkit.base import bare_collections as bc


def annotation(text: str) -> ast.expr:
    """取注解表达式节点。

    Args:
        text: 注解源码（如 `list[int]`）。

    Returns:
        ast.expr: 注解节点。
    """
    node = ast.parse(f"value: {text}").body[0]
    assert isinstance(node, ast.AnnAssign)
    return node.annotation


def test_annotation_matrix(tmp_path: Path) -> None:
    """注解形态矩阵：泛型头名 / 白名单 / 协议 / 裸形态 / 未登记 / 联合 / 字符串引用。"""
    cfg = config(tmp_path)
    base_rule = cfg.collections_rules
    assert base_rule is not None
    rule = type(base_rule)(**{**base_rule.__dict__, "tuple_forms": ("tuple",), "protocol_forms": ("Iterator",)})
    assert bc.annotation_violations(annotation("typing.List[int]"), rule) == ["List"]
    assert bc.annotation_violations(annotation("dict[str, int]"), rule) == ["dict"]
    assert bc.annotation_violations(annotation("tuple[int, ...]"), rule) == ["tuple"]
    assert bc.annotation_violations(annotation("tuple[list[int], str]"), rule) == ["list"]
    assert bc.annotation_violations(annotation("StableList[int]"), rule) == []
    assert bc.annotation_violations(annotation("Iterator[int]"), rule) == []
    assert bc.annotation_violations(annotation("StableList"), rule) == []
    assert bc.annotation_violations(annotation("Iterator"), rule) == []
    assert bc._head_name(ast.parse("a[b]").body[0].value) == ""  # type: ignore[attr-defined]
    assert bc.annotation_violations(annotation("list"), rule) == ["list"]
    assert bc.annotation_violations(annotation("tuple"), rule) == ["bare_tuple"]
    assert bc.annotation_violations(annotation("Sequence"), rule) == ["Sequence"]
    assert bc.annotation_violations(annotation("list[int] | None"), rule) == ["list"]
    assert bc.annotation_violations(annotation('"tuple[int, ...]"'), rule) == ["tuple"]
    assert bc.annotation_violations(annotation('"list[int]"'), rule) == ["list"]
    assert bc.annotation_violations(None, rule) == []


def test_base_name_variants() -> None:
    """基类名解析：属性访问 / 下标头名 / 无法识别。"""

    def base_of(source: str) -> str:
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.ClassDef)
        return bc._base_name(node.bases[0])

    assert base_of("class A(x.BaseObject): pass") == "BaseObject"
    assert base_of("class A(Stable[int]): pass") == "Stable"
    assert base_of("class A((1, 2)): pass") == ""


def test_homogeneous_tuple_edges() -> None:
    """同构 tuple 判定：下标非元组 / 末元素非省略号。"""
    assert bc._is_homogeneous_tuple(annotation("tuple[int]")) is False  # type: ignore[arg-type]
    assert bc._is_homogeneous_tuple(annotation("tuple[int, str]")) is False  # type: ignore[arg-type]


def test_iter_target_files(tmp_path: Path) -> None:
    """扫描目标展开：服务分组按 src / tests 展开，排除目录与重复目标不重复计入。"""
    import dataclasses

    from engkit.config import BackendConfig

    base = config(tmp_path)
    base_rule = base.collections_rules
    assert base_rule is not None
    cfg = dataclasses.replace(
        base,
        backend=BackendConfig(root="backend", groups=("libs", "services"), test_prefix="test_", projects=()),
    )
    write(
        tmp_path,
        {
            "backend/services/one/src/b.py": "VALUE = 1\n",
            "backend/services/one/tests/test_c.py": "VALUE = 1\n",
            "backend/services/one/docs/d.py": "VALUE = 1\n",
            "backend/services/empty/README.md": "x",
            "backend/libs/core/src/__pycache__/cached.py": "VALUE = 1\n",
        },
    )
    service_rule = type(base_rule)(
        **{
            **base_rule.__dict__,
            "scan_targets": ("backend/services", "backend/libs", "backend/services"),
        }
    )
    files = {path.relative_to(tmp_path).as_posix() for path in bc.iter_target_files(cfg, service_rule)}
    assert files == {"backend/services/one/src/b.py", "backend/services/one/tests/test_c.py"}


def test_scan_source_positions_and_unannotated(tmp_path: Path) -> None:
    """位置归类：类字段 / 局部 / 返回值 / 签名参数，未声明注解的参数跳过。"""
    cfg = config(tmp_path)
    rule = cfg.collections_rules
    assert rule is not None
    source = (
        "class Sample:\n"
        "    field: list[int] = []\n\n"
        "    def run(self, plain, typed: dict[str, int]) -> set[int]:\n"
        "        local: list[str] = []\n"
        "        return set()\n"
    )
    hits = bc.scan_source("src/sample.py", source, rule)
    positions = {(hit.line.split(":")[0].strip(), hit.position) for hit in hits}
    assert ("field", bc.POSITION_CLASS_FIELD) in positions
    assert ("def run(self, plain, typed", bc.POSITION_SIGNATURE_PARAM) in positions
    assert ("def run(self, plain, typed", bc.POSITION_SIGNATURE_RETURN) in positions
    assert ("local", bc.POSITION_LOCAL) in positions


def test_check_reason_fallback(tmp_path: Path) -> None:
    """新增命中未登记专属原因时回落到「判违规形态」原因文案。"""
    cfg = config(tmp_path)
    rule = cfg.collections_rules
    assert rule is not None
    write(tmp_path, {"src/mod.py": "def run(x: Sequence[int]) -> None:\n    del x\n"})
    outcome = bc.check(cfg, rule)
    assert outcome.problems
    assert "落白名单" in outcome.problems[0]


def test_check_reason_declared(tmp_path: Path) -> None:
    """容器名登记了专属原因时直接取用（不走兜底文案）。"""
    cfg = config(tmp_path)
    base_rule = cfg.collections_rules
    assert base_rule is not None
    rule = type(base_rule)(**{**base_rule.__dict__, "reasons": {"list": "专属原因"}})
    write(tmp_path, {"src/mod.py": "def run(x: list[int]) -> None:\n    del x\n"})
    outcome = bc.check(cfg, rule)
    assert any("专属原因" in item for item in outcome.problems)


def test_report_prints_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """报告模式打印扫描异常明细（不中断统计）。"""
    cfg = config(tmp_path)
    rule = cfg.collections_rules
    assert rule is not None
    write(tmp_path, {"src/bad.py": "def f(:\n"})
    outcome = bc.report(cfg, rule, as_json=False)
    assert outcome.problems
    assert "扫描异常" in capsys.readouterr().out


def test_self_test_failure_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """自检矩阵有失败项时退 1。"""
    cfg = config(tmp_path)
    rule = cfg.collections_rules
    assert rule is not None
    monkeypatch.setattr(
        bc,
        "check",
        lambda *args, **kwargs: bc.Outcome(problems=["boom"], counts={}, hits=[]),
    )
    assert bc.self_test(rule) == 1
