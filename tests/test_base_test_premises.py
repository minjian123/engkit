"""测试状态前提护栏用例：只补 `self_test` 够不到的分支。

判定向量（P1 整表口径 / P2 逐例重装 / P3 独占库分类 / P4 全局状态锁 / 豁免注释）由
`test_premises.self_test()` 唯一覆盖，本文件**不重复抄写**（口径同 `test_base_test_db_isolation.py`）。
这里覆盖：文件枚举与增量清单、台账导出、缺规则返回 2、基线读写与只减不增、AST 边界分支。

Kiwi 用例号 2556（13_10 测试状态前提静态护栏）。
"""

from __future__ import annotations

import ast
import io
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from base_fixtures import config, write

from engkit.base import test_premises
from engkit.config import parse

pytestmark = pytest.mark.kiwi_id(2556)

P1_BAD = 'def test_a(body):\n    assert body["total"] == 0\n'
P2_BAD = "import pytest\n\n\n@pytest.fixture\nasync def service_app():\n    yield 1\n"
GOOD = "def test_ok(client):\n    assert client is not None\n"
OWN_NO_SCOPE = 'import pytest\n\n\n@pytest.mark.own_db("建库即被测")\ndef test_a():\n    pass\n'
OWN_SCOPED = 'import pytest\n\n\n@pytest.mark.own_db("建库即被测", scope="ddl")\ndef test_b():\n    pass\n'
MODULE_OWN_NO_REASON = "import pytest\n\npytestmark = [pytest.mark.own_db(scope='ddl')]\n"


def test_scan_text_syntax_error_returns_empty(tmp_path: Path) -> None:
    """语法错误文件不崩、返回空清单（解析不了就不判）。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None
    assert test_premises.scan_text("src/tests/test_broken.py", "def (:\n", rule) == []
    assert test_premises.scan_hits("src/tests/test_broken.py", "def (:\n", rule) == []
    assert test_premises.collect_own_db("src/tests/test_broken.py", "def (:\n", rule) == []


def test_run_full_scan_and_incremental(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量扫 `test_*.py` 与 `conftest.py`；剪枝与非目标文件跳过；增量清单支持 Windows 路径。"""
    write(
        tmp_path,
        {
            "src/tests/test_bad.py": P1_BAD,
            "src/tests/test_good.py": GOOD,
            "src/tests/helper.py": P1_BAD,
            "svc/tests/conftest.py": P2_BAD,
            "src/tests/__pycache__/test_skip.py": P1_BAD,
        },
    )
    cfg = config(tmp_path)
    rule = cfg.test_premises_rules
    assert rule is not None
    assert test_premises._iter_files(tmp_path, rule, "") == [
        "src/tests/test_bad.py",
        "src/tests/test_good.py",
        "svc/tests/conftest.py",
    ]

    assert test_premises.run(cfg) == 1
    out = capsys.readouterr().out
    assert "test_bad.py" in out and "conftest.py" in out
    assert "helper.py" not in out and "test_skip.py" not in out

    changed = tmp_path / "changed.txt"
    changed.write_text("src\\tests\\test_good.py\nmissing.py\nsrc/tests/helper.py\n", encoding="utf-8")
    assert test_premises._iter_files(tmp_path, rule, str(changed)) == ["src/tests/test_good.py"]
    assert test_premises.run(cfg, files_from=str(changed)) == 0
    assert "通过" in capsys.readouterr().out

    # 清单来源 `-`：门禁通过管道递清单，不落临时文件
    monkeypatch.setattr(sys, "stdin", io.StringIO("src/tests/test_bad.py\nsrc/tests/helper.py\n"))
    assert test_premises._iter_files(tmp_path, rule, "-") == ["src/tests/test_bad.py"]


def test_run_clean_and_self_test(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """全量通过输出计数；内置判定向量自检通过。"""
    write(tmp_path, {"src/tests/test_good.py": GOOD})
    cfg = config(tmp_path)
    assert test_premises.run(cfg) == 0
    assert "通过：" in capsys.readouterr().out
    assert test_premises.run(cfg, as_self_test=True) == 0


def test_self_test_failure_branch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """自测量按命中结果比对：判定器返回空时自检不通过（覆盖失败出口）。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None
    original = test_premises.scan_hits
    try:
        test_premises.scan_hits = lambda rel_path, text, rule: []  # type: ignore[assignment]
        assert test_premises.self_test(rule) == 1
    finally:
        test_premises.scan_hits = original
    assert "不通过" in capsys.readouterr().out


def test_list_own_db_exports_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """台账导出：缺 scope / 缺理由逐条标注，合法 scope 原样输出。"""
    write(
        tmp_path,
        {
            "src/tests/test_own.py": OWN_NO_SCOPE,
            "src/tests/test_scoped.py": OWN_SCOPED,
            "src/tests/test_module.py": MODULE_OWN_NO_REASON,
        },
    )
    cfg = config(tmp_path)
    assert test_premises.run(cfg, list_own_db=True) == 0
    out = capsys.readouterr().out
    assert "test_own.py::test_a::缺 scope::建库即被测" in out
    assert "test_scoped.py::test_b::ddl::建库即被测" in out
    assert "test_module.py::<module>::ddl::缺理由" in out

    rule = cfg.test_premises_rules
    assert rule is not None
    mixed = (
        "import pytest\n\n\n@decorator\ndef f():\n    pass\n\n\n"
        '@pytest.mark.own_db("台账", scope="seed")\ndef g():\n    pass\n'
    )
    assert test_premises.collect_own_db("src/tests/test_mixed.py", mixed, rule) == [
        "src/tests/test_mixed.py::g::seed::台账"
    ]


def test_run_without_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """未登记规则返回 2 并提示键位。"""
    raw = {
        "docs": {"root": "docs"},
        "rules": {"test_db_isolation": {"scan_targets": ["src"]}},
    }
    cfg = parse(raw, tmp_path / "engkit.toml")
    assert test_premises.run(cfg) == 2
    assert "test_premises" in capsys.readouterr().out


def test_update_baseline_requires_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """规则未登记基线路径时不得静默改写，退 2 并提示。"""
    write(tmp_path, {"src/tests/test_bad.py": P1_BAD})
    cfg = config(tmp_path)
    rule = cfg.test_premises_rules
    assert rule is not None
    scoped = replace(cfg, test_premises_rules=replace(rule, baseline=""))
    assert test_premises.run(scoped, update_baseline=True) == 2
    assert "未配置 baseline" in capsys.readouterr().out


def test_baseline_update_shrink_and_excess(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """基线只减不增：超基线判失败、少于基线给递减提示、重写后回到通过。"""
    write(tmp_path, {"src/tests/test_a.py": P1_BAD, "src/tests/test_b.py": P1_BAD})
    cfg = config(tmp_path)
    baseline = tmp_path / "premises_baseline.json"

    assert test_premises.run(cfg, update_baseline=True) == 0
    assert "存量基线已更新" in capsys.readouterr().out
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "entries": [
            {"file": "src/tests/test_a.py", "code": "P1", "count": 1},
            {"file": "src/tests/test_b.py", "code": "P1", "count": 1},
        ],
    }

    # 等于基线（仍有存量）⇒ 通过并说明存量在基线内
    assert test_premises.run(cfg) == 0
    assert "存量 2 项仍在基线内" in capsys.readouterr().out

    # 同一文件多出两处命中 ⇒ 超基线，只报超出的那个文件
    write(tmp_path, {"src/tests/test_a.py": P1_BAD + '    assert body["items"] == []\n    assert body["rows"] == ()\n'})
    assert test_premises.run(cfg) == 1
    excess_out = capsys.readouterr().out
    assert "新增 2 项" in excess_out and "test_a.py" in excess_out and "test_b.py" not in excess_out

    # 改好后少于基线 ⇒ 通过 + 递减提示
    write(tmp_path, {"src/tests/test_a.py": GOOD})
    assert test_premises.run(cfg) == 0
    assert "--update-baseline" in capsys.readouterr().out

    assert test_premises.run(cfg, update_baseline=True) == 0
    assert json.loads(baseline.read_text(encoding="utf-8"))["entries"] == [
        {"file": "src/tests/test_b.py", "code": "P1", "count": 1}
    ]
    capsys.readouterr()


@pytest.mark.parametrize(
    ("payload", "keyword"),
    [
        ("not json", "不可解析"),
        (json.dumps({"version": 99, "entries": []}), "版本非法"),
        (json.dumps({"version": 1}), "entries 非法"),
        (json.dumps({"version": 1, "entries": [1]}), "条目非法"),
        (json.dumps({"version": 1, "entries": [{"file": "src/tests/test_bad.py"}]}), "字段缺失或非法"),
        (
            json.dumps({"version": 1, "entries": [{"file": "src/tests/test_bad.py", "code": "P1", "count": 0}]}),
            "count 非法",
        ),
    ],
)
def test_baseline_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: str, keyword: str) -> None:
    """基线快照格式 / 版本 / 字段非法一律退 2，不静默当成空基线。"""
    write(tmp_path, {"src/tests/test_bad.py": P1_BAD, "premises_baseline.json": payload})
    assert test_premises.run(config(tmp_path)) == 2
    assert keyword in capsys.readouterr().out


def test_ast_helpers_edges(tmp_path: Path) -> None:
    """AST 取名的空分支、空字面量判定、模块标记识别与豁免注释格式。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None

    assert test_premises._call_name(ast.Constant(value=1)) == ""
    assert test_premises._call_name(ast.parse("f()").body[0].value) == "f"  # type: ignore[attr-defined]
    subscript_call = ast.Call(func=ast.Subscript(value=ast.Name(id="a"), slice=ast.Constant(1)), args=[], keywords=[])
    assert test_premises._call_name(subscript_call) == ""

    empty_list = ast.parse("[]").body[0].value  # type: ignore[attr-defined]
    empty_dict = ast.parse("{}").body[0].value  # type: ignore[attr-defined]
    empty_tuple = ast.parse("()").body[0].value  # type: ignore[attr-defined]
    empty_set = ast.parse("set()").body[0].value  # type: ignore[attr-defined]
    non_empty = ast.parse("[1]").body[0].value  # type: ignore[attr-defined]
    assert test_premises._is_empty_literal(empty_list)
    assert test_premises._is_empty_literal(empty_dict)
    assert test_premises._is_empty_literal(empty_tuple)
    assert test_premises._is_empty_literal(empty_set)
    assert not test_premises._is_empty_literal(non_empty)
    assert not test_premises._is_empty_literal(ast.Constant(value=0))

    module = ast.parse(
        "import pytest\n\npytestmark: list[object] = [pytest.mark.integration]\nother = [pytest.mark.integration]\n"
    )
    assert [type(entry).__name__ for entry in test_premises._pytestmark_entries(module)] == ["Attribute"]
    assert test_premises._pytestmark_has(module, "integration")
    assert not test_premises._pytestmark_has(ast.parse("x = 1\n"), "integration")
    assert not test_premises._pytestmark_has(ast.parse("import pytest\npytestmark = [1]\n"), "integration")

    plain = ast.parse("def f():\n    pass\n").body[0]
    assert test_premises._decorator_attrs(plain) == []
    plain_decorated = ast.parse("@decorator\ndef f():\n    pass\n").body[0]
    assert test_premises._decorator_attrs(plain_decorated) == []
    decorated = ast.parse("@pytest.mark.integration()\ndef f():\n    pass\n").body[0]
    assert test_premises._decorator_attrs(decorated) == ["integration"]
    bare = ast.parse("@pytest.mark.integration\ndef f():\n    pass\n").body[0]
    assert test_premises._decorator_attrs(bare) == ["integration"]
    assert (
        test_premises._marker_call(ast.parse("@decorator\ndef f():\n    pass\n").body[0].decorator_list[0], "own_db")
        is None
    )
    non_str_scope = ast.parse("f(scope=1)").body[0].value  # type: ignore[attr-defined]
    assert test_premises._marker_scope(non_str_scope) == ""
    no_args = ast.parse("f()").body[0].value  # type: ignore[attr-defined]
    assert test_premises._marker_reason(no_args) == ""
    non_str_reason = ast.parse("f(123)").body[0].value  # type: ignore[attr-defined]
    assert test_premises._marker_reason(non_str_reason) == ""
    assert test_premises._pytestmark_calls(ast.parse("pytestmark = [1]\n"), "own_db") == []
    assert test_premises._pytestmark_calls(ast.parse("pytestmark = [f()]\n"), "own_db") == []

    scanner = test_premises._Scanner(
        "src/tests/test_x.py",
        rule,
        [
            "value = 'premise: own-data 字符串里的不算'",
            "# premise: own-data",
            "assert body['total'] == 0  # premise: own-data 行尾说明",
        ],
    )
    assert scanner._header_lines(ast.parse("def f():\n    pass\n").body[0]) == range(1, 2)
    assert scanner._premise_kind(99) == ""
    assert scanner._premise_kind(1) == ""
    assert scanner._premise_kind(2) == ""
    assert scanner._premise_kind(3) == "own-data"


def test_premise_scan_variants(tmp_path: Path) -> None:
    """P1：只认「字段 ↔ 字面空值」等值比较，反向比较同判，其它形态不误判。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None

    def hits(source: str) -> list[str]:
        return [hit.code for hit in test_premises.scan_hits("src/tests/test_x.py", source, rule)]

    assert hits("assert 1 == 1\n") == []
    assert hits('assert body["total"] != 0\n') == []
    assert hits('assert body["total"] < 1\n') == []
    assert hits('assert body["total"] == False\n') == []
    assert hits("assert body[0] == 0\n") == []
    assert hits('assert body["total"] == 3\n') == []
    assert hits('assert [] == body["items"]\n') == ["P1"]
    assert hits('assert body["count"] == 0\n') == ["P1"]
    assert hits('assert body["data"]["total"] == 0\n') == ["P1"]
    assert hits('assert body["rows"] == ()\n') == ["P1"]
    assert hits('assert body["rows"] == {}\n') == ["P1"]
    assert hits('assert body["rows"] == set()\n') == ["P1"]


def test_fixture_scope_variants(tmp_path: Path) -> None:
    """P2：scope 非字符串按函数级判，非装配夹具名不判。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None

    def hits(source: str) -> list[str]:
        return [hit.code for hit in test_premises.scan_hits("svc/tests/conftest.py", source, rule)]

    assert hits('import pytest\n\n\n@pytest.fixture(scope="session")\nasync def service_app():\n    yield 1\n') == []
    assert hits('import pytest\n\n\n@pytest.fixture(scope="class")\nasync def service_app():\n    yield 1\n') == ["P2"]
    assert hits("import pytest\n\n\n@pytest.fixture(scope=1)\nasync def service_app():\n    yield 1\n") == ["P2"]
    assert hits("import pytest\n\n\n@pytest.fixture\nasync def other_app():\n    yield 1\n") == []


def test_own_marker_variants(tmp_path: Path) -> None:
    """P3：类级标记同判，`external` 允许函数装饰器或模块级裸标记形式的 integration。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None

    def hits(source: str) -> list[str]:
        return [hit.code for hit in test_premises.scan_hits("svc/tests/test_x.py", source, rule)]

    assert hits('import pytest\n\n\n@pytest.mark.own_db("类级")\nclass TestX:\n    pass\n') == ["P3"]
    assert (
        hits(
            "import pytest\n\n\n@pytest.mark.integration\n"
            '@pytest.mark.own_db("真机联调", scope="external")\ndef test_a():\n    pass\n'
        )
        == []
    )
    assert (
        hits(
            "import pytest\n\npytestmark = pytest.mark.integration\n\n\n"
            '@pytest.mark.own_db("真机联调", scope="external")\ndef test_a():\n    pass\n'
        )
        == []
    )
    assert hits('import pytest\n\n\n@pytest.mark.own_db("非字符串 scope", scope=1)\ndef test_a():\n    pass\n') == [
        "P3"
    ]


def test_lock_variants(tmp_path: Path) -> None:
    """P4：只认「锁名是字面常量且在禁用清单」的调用，其余形态不误判。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None

    def hits(source: str) -> list[str]:
        return [hit.code for hit in test_premises.scan_hits("svc/tests/test_x.py", source, rule)]

    assert hits("def test_a():\n    file_lock()\n") == []
    assert hits("def test_a():\n    file_lock(name)\n") == []
    assert hits("def test_a():\n    file_lock('other-lock')\n") == []
    assert hits("def test_a():\n    locks.file_lock('platform-global-state')\n") == ["P4"]


def test_scan_targets_missing(tmp_path: Path) -> None:
    """扫描根不存在时返回空清单（不因仓结构差异报错）。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None
    scoped = replace(rule, scan_targets=("absent_dir",))
    assert test_premises._iter_files(tmp_path, scoped, "") == []


def test_tally_and_write_baseline(tmp_path: Path) -> None:
    """计数同文件同规则码累加；写基线按文件 + 规则码排序。"""
    rule = config(tmp_path).test_premises_rules
    assert rule is not None
    hits = [
        test_premises.Hit("b.py", test_premises.P1, 1, "x"),
        test_premises.Hit("b.py", test_premises.P1, 2, "x"),
        test_premises.Hit("a.py", test_premises.P4, 3, "x"),
    ]
    assert test_premises.tally(hits) == {("b.py", "P1"): 2, ("a.py", "P4"): 1}
    path = tmp_path / "nested" / "baseline.json"
    assert test_premises.write_baseline(path, hits, rule) == 2
    assert json.loads(path.read_text(encoding="utf-8"))["entries"] == [
        {"file": "a.py", "code": "P4", "count": 1},
        {"file": "b.py", "code": "P1", "count": 2},
    ]
