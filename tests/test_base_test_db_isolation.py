"""测试库隔离护栏用例：只补 `self_test` 够不到的分支。

判定向量（连接串字面量 / 引擎构造 / 裸 int ID / 无 where 清理 / 白名单 / 注释不误判）
由 `test_db_isolation.self_test()` 唯一覆盖，本文件**不重复抄写**（2026-11-01 拍板）。
这里覆盖：文件枚举与增量清单、白名单导出、缺规则返回 2、`ast.parse` 语法错误、`run` 三种出口。
"""

from __future__ import annotations

import ast
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from base_fixtures import config, write

from engkit.base import test_db_isolation
from engkit.config import parse

BAD = 'def test_a():\n    url = "sqlite+aiosqlite:///x.db"\n'
GOOD = "def test_b(client):\n    assert client is not None\n"
OWN = (
    "import pytest\n\n\n"
    '@pytest.mark.own_db("建删库是被测功能")\n'
    "def test_create_db(tmp_path):\n"
    '    url = "sqlite+aiosqlite:///" + str(tmp_path / "x.db")\n'
)
OWN_NO_REASON = (
    "import pytest\n\n\n@pytest.mark.own_db\n" + "def test_own(tmp_path):\n" + '    url = "sqlite:///x.db"\n'
)


def test_scan_text_syntax_error_returns_empty(tmp_path: Path) -> None:
    """语法错误文件不崩、返回空清单（解析不了就不判）。"""
    rule = config(tmp_path).test_db_isolation_rules
    assert rule is not None
    assert test_db_isolation.scan_text("src/tests/test_broken.py", "def (:\n", rule) == []
    assert test_db_isolation.collect_own_db("src/tests/test_broken.py", "def (:\n", rule) == []


def test_run_full_scan_and_incremental(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """全量只扫 `test_*.py`、剪枝目录跳过；增量清单支持 Windows 路径与非目标文件。"""
    write(
        tmp_path,
        {
            "src/tests/test_bad.py": BAD,
            "src/tests/test_good.py": GOOD,
            "src/tests/helper.py": BAD,
            "svc/tests/test_bad.py": BAD,
            "src/tests/__pycache__/test_skip.py": BAD,
        },
    )
    cfg = config(tmp_path)
    assert test_db_isolation.run(cfg) == 1
    out = capsys.readouterr().out
    assert "test_bad.py" in out and "helper.py" not in out and "test_skip.py" not in out

    changed = tmp_path / "changed.txt"
    changed.write_text("src\\tests\\test_good.py\nmissing.py\n", encoding="utf-8")
    assert test_db_isolation.run(cfg, files_from=str(changed)) == 0
    assert "通过" in capsys.readouterr().out

    changed.write_text("src/tests/helper.py\n", encoding="utf-8")
    assert test_db_isolation.run(cfg, files_from=str(changed)) == 0
    assert "通过" in capsys.readouterr().out


def test_run_clean_and_self_test(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """全量通过输出计数；内置判定向量自检通过。"""
    write(tmp_path, {"src/tests/test_good.py": GOOD})
    cfg = config(tmp_path)
    assert test_db_isolation.run(cfg) == 0
    assert "通过" in capsys.readouterr().out
    assert test_db_isolation.run(cfg, as_self_test=True) == 0


def test_list_own_db_exports_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """白名单导出：带理由原样输出，缺理由标注「缺理由」。"""
    write(tmp_path, {"src/tests/test_own.py": OWN, "src/tests/test_own2.py": OWN_NO_REASON})
    cfg = config(tmp_path)
    assert test_db_isolation.run(cfg, list_own_db=True) == 0
    out = capsys.readouterr().out
    assert "建删库是被测功能" in out
    assert "缺理由" in out


def test_run_without_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """未登记规则返回 2 并提示键位。"""
    raw = tomllib.loads("[docs]\nroot = 'docs'\n")
    cfg = parse(raw, tmp_path / "engkit.toml")
    assert test_db_isolation.run(cfg) == 2
    assert "test_db_isolation" in capsys.readouterr().out


EDGES = '''import pytest
from pathlib import Path
from sqlalchemy import delete, select


@pytest.mark.other()
@pytest.mark.own_db("多装饰器仍放行")
def test_two_markers():
    pass


@decorator
def test_plain_decorator():
    pass


@pytest.mark.other
def test_other_marker():
    pass


@pytest.mark.other()
def test_other_marker_call():
    pass


@pytest.mark.own_db("整类自建库")
class TestOwn:
    """类级白名单作用域（覆盖 ClassDef 分支）。"""

    def test_in_class(self, tmp_path):
        path = tmp_path / "y.db"


@pytest.mark.own_db()
def test_marker_no_args(tmp_path):
    path = tmp_path / "x.txt"


@pytest.mark.own_db(123)
def test_marker_non_str():
    pass


async def test_async_sql(session):
    await session.execute()
    await session.execute(1)
    await session.execute("SELECT 1")
    await session.execute("SELECT 1 WHERE id = :id")
    await session.execute("SELECT 1 WHERE id = :id", {"id": sqlite_id(1)})
    await session.execute("SELECT 1 WHERE id = :id", other)
    (lambda: 1)()


def test_cleanup(meta, table, cache):
    meta.execute(delete(table))
    cache.delete("k")
'''


def test_module_pytestmark_and_base_modules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """模块级 `pytestmark` 放行；共享基座模块（`base_modules`）不参与判定。"""
    module_mark = (
        "import pytest\n\n"
        'pytestmark = [\n    pytest.mark.kiwi_id(1),\n    pytest.mark.own_db("整份文件的建库即被测"),\n]\n\n\n'
        "def test_a(tmp_path):\n    e = create_async_engine('x')\n"
    )
    plain_mark = (
        "import pytest\n\npytestmark = pytest.mark.kiwi_id(1)\n\n\ndef test_b(tmp_path):\n    p = tmp_path / 'x.db'\n"
    )
    no_arg_mark = (
        "import pytest\n\npytestmark = [pytest.mark.own_db()]\n\n\ndef test_c(tmp_path):\n    p = tmp_path / 'x.db'\n"
    )
    annotated = (
        "import pytest\n\npytestmark: list[object] = [pytest.mark.own_db('标注式')]\n\n\ndef test_d():\n    pass\n"
    )
    non_call_element = "import pytest\n\npytestmark = [1]\n"
    non_str_reason = "import pytest\n\npytestmark = [pytest.mark.own_db(123)]\n"
    rule = config(tmp_path).test_db_isolation_rules
    assert rule is not None

    assert test_db_isolation.scan_text("src/tests/test_mod.py", module_mark, rule) == []
    assert test_db_isolation.collect_own_db("src/tests/test_mod.py", module_mark, rule) == [
        "src/tests/test_mod.py::<module>::整份文件的建库即被测"
    ]
    assert test_db_isolation._pytestmark_reason(ast.parse(plain_mark), "own_db") == ""
    assert test_db_isolation._pytestmark_reason(ast.parse(no_arg_mark), "own_db") == ""
    assert test_db_isolation._pytestmark_reason(ast.parse(annotated), "own_db") == "标注式"
    assert test_db_isolation._pytestmark_reason(ast.parse(GOOD), "own_db") == ""
    assert test_db_isolation._pytestmark_reason(ast.parse(non_call_element), "own_db") == ""
    assert test_db_isolation._pytestmark_reason(ast.parse(non_str_reason), "own_db") == ""

    # 内存库豁免：字面量形式 / `:memory:` 形式 / 无参引擎构造（非内存库，仍判）
    assert test_db_isolation.scan_text("src/tests/test_mem.py", 'X = "sqlite://"\n', rule) == []
    assert test_db_isolation.scan_text("src/tests/test_mem.py", 'X = "sqlite:///:memory:"\n', rule) == []
    no_args = "def test_c():\n    e = create_engine()\n"
    assert len(test_db_isolation.scan_text("src/tests/test_noargs.py", no_args, rule)) == 1
    assert len(test_db_isolation.scan_text("src/tests/test_plain.py", plain_mark, rule)) == 1

    write(tmp_path, {"src/tests/test_base.py": BAD, "src/tests/test_other.py": BAD})
    scoped = replace(rule, base_modules=("src/tests/test_base.py",))
    assert test_db_isolation._iter_files(tmp_path, scoped, "") == ["src/tests/test_other.py"]
    changed = tmp_path / "changed.txt"
    changed.write_text("src/tests/test_base.py\nsrc/tests/test_other.py\n", encoding="utf-8")
    assert test_db_isolation._iter_files(tmp_path, scoped, str(changed)) == ["src/tests/test_other.py"]
    capsys.readouterr()


def test_edge_branches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """装饰器 / 非调用 / 原生 SQL 早退 / 非 where 删除等边界分支。"""
    rule = config(tmp_path).test_db_isolation_rules
    assert rule is not None
    problems = test_db_isolation.scan_text("src/tests/test_edges.py", EDGES, rule)
    assert any("delete()" in item for item in problems)
    assert test_db_isolation.scan_text("src/tests/test_sel.py", "from sqlalchemy import select\n", rule) == []
    assert test_db_isolation.collect_own_db("src/tests/test_edges.py", EDGES, rule) == [
        "src/tests/test_edges.py::test_two_markers::多装饰器仍放行",
        "src/tests/test_edges.py::TestOwn::整类自建库",
        "src/tests/test_edges.py::test_marker_no_args::缺理由",
        "src/tests/test_edges.py::test_marker_non_str::缺理由",
    ]

    assert test_db_isolation._call_name(ast.Constant(value=1)) == ""
    subscript_call = ast.Call(func=ast.Subscript(value=ast.Name(id="a"), slice=ast.Constant(1)), args=[], keywords=[])
    assert test_db_isolation._call_name(subscript_call) == ""

    write(tmp_path, {"src/tests/test_good.py": GOOD})
    assert test_db_isolation.run(config(tmp_path)) == 0
    missing_rule = replace(rule, scan_targets=("absent_dir",))
    assert test_db_isolation._iter_files(tmp_path, missing_rule, "") == []
    capsys.readouterr()
