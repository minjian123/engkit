"""基座校验器的失败 / 跳过分支用例（补齐各器分支覆盖率）。

主流程见 `test_base_check.py`；本文件针对每器的「结构非法 / 清单不符 / 目录形态异常」等
不常走到的分支逐条断言，保证行与分支覆盖率达标（口径见《AI开发规范》「覆盖率要求」节）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from base_fixtures import backend_repo, config, write

from engkit.base import backend_base


def test_parse_chains_branches() -> None:
    """链路解析：全大写段 / 长度不一 / 正常配对。"""
    assert backend_base.parse_chains("`ABC` → `DEF`") == []
    assert backend_base.parse_chains("`Ab` / `Bc` / `Cd` → `De` / `Ef`") == []
    assert backend_base.parse_chains("`BaseMid` / `BaseOther` → `BaseObj`") == [
        ["BaseMid", "BaseObj"],
        ["BaseOther", "BaseObj"],
    ]


def test_missing_source_dir_and_chain_class(tmp_path: Path) -> None:
    """源码面缺目录即跳过；清单登记的起始链类在代码中不存在即报。"""
    backend_repo(tmp_path)
    write(tmp_path, {"docs/契约.md": "## 7. 继承链与代码位置\n\n- `Ghost → BaseObj`。\n\n体系根清单\n\n- `BaseObj`\n"})
    cfg = config(tmp_path)
    rule = cfg.backend_base_rules
    outcome, _lines = backend_base.collect(cfg, rule)
    assert any("代码未找到该类" in item for item in outcome.problems)
    widened = rule.__class__(**{**rule.__dict__, "source_dirs": ("src", "ghost_dir")})
    outcome, _lines = backend_base.collect(cfg, widened)
    assert any("代码未找到该类" in item for item in outcome.problems)


def test_marker_missing(tmp_path: Path) -> None:
    """实现契约缺「体系根清单」小节即白名单缺失。"""
    backend_repo(tmp_path)
    write(tmp_path, {"docs/契约.md": "## 7. 继承链与代码位置\n\n- `BaseMid → BaseObj`。\n"})
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    assert any("白名单缺失" in item for item in outcome.problems)


def test_descendants_cycle(tmp_path: Path) -> None:
    """继承图存在环时子类集合不重复展开（去重分支）。"""
    backend_repo(tmp_path)
    write(
        tmp_path,
        {
            "src/pkg/cycle.py": (
                "class Cyc(BaseObject):\n    pass\n\n\nclass Cyc2(Cyc):\n    pass\n\n\nclass Cyc(Cyc2):\n    pass\n"
            )
        },
    )
    cfg = config(tmp_path)
    index = backend_base.index_classes(cfg, cfg.backend_base_rules)
    assert {"Cyc", "Cyc2"} <= backend_base.descendants(index, "BaseObject")


def test_class_index_skips_tests_dir(tmp_path: Path) -> None:
    """测试目录下的类不参与继承索引（剪枝口径）。"""
    backend_repo(tmp_path)
    write(tmp_path, {"src/pkg/tests/test_x.py": "class Helper(BaseObject):\n    pass\n"})
    cfg = config(tmp_path)
    index = backend_base.index_classes(cfg, cfg.backend_base_rules)
    assert "Helper" not in index


def test_inheritance_mismatch_and_manifest(tmp_path: Path) -> None:
    """继承链父类不符 / 直接继承未登记清单。"""
    backend_repo(tmp_path)
    write(
        tmp_path,
        {
            "src/pkg/base.py": (
                "class BaseObj(object):\n    pass\n\n\n"
                "class BaseMid(BaseObj):\n    pass\n\n\n"
                "class Direct(BaseObject):\n    pass\n"
            )
        },
    )
    cfg = config(tmp_path)
    rule = cfg.backend_base_rules
    outcome, _lines = backend_base.collect(cfg, rule)
    assert any("[清单对账]" in item for item in outcome.problems)
    assert any("[直继承]" in item for item in outcome.problems)

    write(tmp_path, {"docs/契约.md": "## 7. 继承链与代码位置\n\n- `BaseMid → Direct`。\n\n体系根清单\n\n- `BaseObj`\n"})
    outcome, _lines = backend_base.collect(cfg, rule)
    assert any("应为 `Direct` 子类" in item for item in outcome.problems)


def test_root_bases_section_break(tmp_path: Path) -> None:
    """体系根小节后出现非清单行即停止解析（白名单缺失按拒绝处理）。"""
    backend_repo(tmp_path)
    write(
        tmp_path,
        {
            "docs/契约.md": (
                "## 7. 继承链与代码位置\n\n- `BaseMid → BaseObj`。\n\n体系根清单\n\n- 见下\nX 说明文字\n- `BaseObj`\n"
            )
        },
    )
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    assert any("白名单缺失" in item for item in outcome.problems)


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ("{ broken", "基线快照不可解析"),
        (json.dumps({"version": 2}), "基线快照版本非法"),
        (json.dumps({"version": 1, "entries": {}}), "基线快照 entries 非法"),
        (json.dumps({"version": 1, "entries": ["x"]}), "基线快照条目非法"),
    ),
)
def test_direct_baseline_errors(tmp_path: Path, payload: str, message: str) -> None:
    """直继承基线的四类结构非法各记一条明细。"""
    backend_repo(tmp_path)
    write(tmp_path, {"baseline.json": payload})
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    assert any(message in item for item in outcome.problems)


def test_direct_inheritance_dedup(tmp_path: Path) -> None:
    """同一文件内重复定义同一类只登记一次（去重分支）。"""
    backend_repo(tmp_path)
    write(
        tmp_path,
        {
            "docs/契约.md": "## 7. 继承链与代码位置\n\n- `BaseMid → BaseObj`。\n\n体系根清单\n\n- `BaseObj`\n",
            "docs/基类清单.md": "| BaseObj | 体系根 |\n| Twin | 存量 |\n",
            "src/pkg/twin.py": "class Twin(BaseObject):\n    pass\n\n\nclass Twin(BaseObject):\n    pass\n",
            "baseline.json": json.dumps({"version": 1, "entries": [{"file": "src/pkg/twin.py", "class": "Twin"}]}),
        },
    )
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    assert not any("Twin" in item for item in outcome.problems)


def test_data_class_semantics(tmp_path: Path) -> None:
    """数据类语义：值对象须 frozen、框架对象不得 dataclass、其它修饰器跳过 / 语法错误跳过。"""
    backend_repo(tmp_path)
    write(
        tmp_path,
        {
            "src/pkg/shapes.py": (
                "class BaseValueObject(object):\n    pass\n\n\n"
                "class BaseFrameworkObject(object):\n    pass\n\n\n"
                "class ValueChild(BaseValueObject):\n    pass\n\n\n"
                "@dataclass\n"
                "class LooseChild(BaseValueObject):\n    pass\n\n\n"
                "@dataclass(frozen=True)\n"
                "class OkChild(BaseValueObject):\n    pass\n\n\n"
                "@dataclass(frozen=True)\n"
                "class BadFramework(BaseFrameworkObject):\n    pass\n\n\n"
                "@staticmethod\n"
                "class Odd(BaseValueObject):\n    pass\n"
            ),
            "src/pkg/broken.py": "class X(:\n",
        },
    )
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    assert any("LooseChild" in item and "须声明" in item for item in outcome.problems)
    assert any("BadFramework" in item and "不得为 dataclass" in item for item in outcome.problems)
    assert any("Odd" in item for item in outcome.problems)


def test_error_code_segment(tmp_path: Path) -> None:
    """错误码：非 5 位平台码即报。"""
    backend_repo(tmp_path)
    write(tmp_path, {"src/errors.py": "class ErrorCode:\n    BIG = 100001\n"})
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    assert any("不是 5 位平台码" in item for item in outcome.problems)


def test_known_domains_variants(tmp_path: Path) -> None:
    """域布局：未配置 / 缺文件 / JSON 非法 / 非对象 / 正常登记。"""
    backend_repo(tmp_path)
    cfg = config(tmp_path)
    rule = cfg.backend_base_rules
    assert backend_base.known_domains(cfg, rule.__class__(**{**rule.__dict__, "domain_layout": ""})) is None
    assert backend_base.known_domains(cfg, rule.__class__(**{**rule.__dict__, "domain_layout": "missing.json"})) is None
    write(tmp_path, {"deploy/domain_layout.json": "{ broken"})
    assert backend_base.known_domains(cfg, rule) is None
    write(tmp_path, {"deploy/domain_layout.json": "[]"})
    assert backend_base.known_domains(cfg, rule) == {"archive"}
    write(tmp_path, {"deploy/domain_layout.json": json.dumps({"domains": [{"domain_key": "alpha"}, "x", {"k": 1}]})})
    assert backend_base.known_domains(cfg, rule) == {"archive", "alpha"}


def test_versions_layout_branches(tmp_path: Path) -> None:
    """版本目录形态：根级脚本 / 域级脚本 / 非法数据源段 / 未登记域 / 隐藏目录。"""
    backend_repo(tmp_path)
    write(
        tmp_path,
        {
            "deploy/domain_layout.json": json.dumps(
                {"domains": [{"domain_key": "platform"}, {"domain_key": "tenant"}]}
            ),
            "versions/stray.py": "revision = '0000'\n",
            "versions/readme.md": "",
            "versions/.hidden/keep.txt": "",
            "versions/tenant/tenant/0001_init.py": (
                'revision = "0001"\ndown_revision = None\nbranch_labels = ("tenant:tenant",)\n'
            ),
            "versions/tenant/direct.py": 'revision = "0000"\n',
            "versions/unknown_domain/platform/x.py": "revision = '0009'\n",
            "versions/tenant/other/readme.md": "",
            "versions/tenant/tenant/__pycache__/cached.py": "revision = '0010'\n",
            "versions/tenant/.hidden/keep.txt": "",
            "versions/tenant/readme.md": "",
        },
    )
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    joined = "\n".join(outcome.problems)
    assert "位于版本根目录" in joined
    assert "未在域布局声明内" in joined
    assert "位于域目录" in joined
    assert "数据源段非法" in joined


def test_versions_dir_missing(tmp_path: Path) -> None:
    """版本目录缺失即报（链检查跳过）。"""
    backend_repo(tmp_path)
    (tmp_path / "versions/platform/tenant/0001_init.py").unlink()
    (tmp_path / "versions").rename(tmp_path / "versions_bak")
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    assert any("未找到 versions/" in item for item in outcome.problems)


def test_chain_branches(tmp_path: Path) -> None:
    """迁移链：缺 revision / revision 重复 / 链首无标签 / 非链首带标签 / ini 缺段 / 空链跳过。"""
    backend_repo(tmp_path)
    write(
        tmp_path,
        {
            "versions/platform/tenant/0001_init.py": 'revision = "0001"\ndown_revision = None\nbranch_labels = None\n',
            "versions/platform/tenant/0002_dup.py": (
                'revision = "0001"\ndown_revision = "0001"\nbranch_labels = ("platform:tenant",)\n'
            ),
            "versions/platform/tenant/0003_norev.py": "VALUE = 1\n",
            "versions/platform/tenant/notes.txt": "",
            "versions/platform/platform/0001_init.py": (
                'revision = "0001"\ndown_revision = None\nbranch_labels = ("platform:platform",)\n'
            ),
            "versions/tenant/tenant/0001_init.py": 'revision = "0001"\ndown_revision = None\nbranch_labels = None\n',
            "versions/tenant/tenant/0002_broken.py": 'revision = "0002"\ndown_revision = "ghost"\n',
            "alembic.ini": "[alembic:other]\n",
        },
    )
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    joined = "\n".join(outcome.problems)
    assert "缺少 revision 定义" in joined
    assert "revision「0001」重复" in joined
    assert "须声明 branch_labels" in joined
    assert "非链首不应声明 branch_labels" in joined
    assert "不在本链（断链）" in joined
    assert "未在 alembic.ini 登记配置段" in joined


def test_empty_versions_prints_hint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """版本目录为空时打印跳过提示（不报违规）。"""
    backend_repo(tmp_path)
    (tmp_path / "versions/platform/tenant/0001_init.py").unlink()
    cfg = config(tmp_path)
    outcome, _lines = backend_base.collect(cfg, cfg.backend_base_rules)
    assert not any("[迁移链]" in item for item in outcome.problems)
    assert "迁移版本目录为空" in capsys.readouterr().out


def test_self_test_failure_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """自检矩阵有失败项时退 1。"""
    backend_repo(tmp_path)
    cfg = config(tmp_path)
    monkeypatch.setattr(backend_base, "ERROR_CODE_RE", __import__("re").compile(r"$^"))
    assert backend_base.run(cfg, as_self_test=True, update_baseline=False) == 1
