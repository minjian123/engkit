"""其余基座校验器的分支用例（原型复核 / 系统字段 / 环境模板 / 规则助手 / 配置解析 / CLI）。

覆盖各器的「结构非法 / 缺证据 / 目录缺失 / 超过清单上限」等分支，与 `test_base_check.py`
的主流程用例互补（覆盖率口径见《AI开发规范》「覆盖率要求」节）。
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from base_fixtures import RULES_TOML, config, write

from engkit import cli
from engkit.base import env_example, prototype_review, rules, system_fields
from engkit.base.rules import RulesError
from engkit.config import Config, ConfigError, EnvExampleRules, EnvTemplateRule, RegistrySpec, parse


def test_rules_helpers(tmp_path: Path) -> None:
    """规则助手：路径解析 / 必填文件 / JSON 读取 / 相对路径 / 按行读。"""
    cfg = config(tmp_path)
    assert rules.path_of(cfg, "docs") == tmp_path / "docs"
    write(tmp_path, {"a.txt": "1\n2\n", "ok.json": '{"version": 1}', "bad.json": "{ broken"})
    assert rules.require_file(cfg, "a.txt", "样例").name == "a.txt"
    with pytest.raises(RulesError, match="样例缺失"):
        rules.require_file(cfg, "missing.txt", "样例")
    assert rules.read_json(tmp_path / "ok.json", "样例") == {"version": 1}
    with pytest.raises(RulesError, match="缺失"):
        rules.read_json(tmp_path / "none.json", "样例")
    with pytest.raises(RulesError, match="不是合法 JSON"):
        rules.read_json(tmp_path / "bad.json", "样例")
    assert rules.rel_posix(tmp_path / "a.txt", tmp_path) == "a.txt"
    assert rules.read_lines(tmp_path / "a.txt") == ["1", "2"]


def test_env_example_template_removed_keys(tmp_path: Path) -> None:
    """模板级 removed_keys 参与残留判定，缺键位与真实私钥各报一条。"""
    rule = EnvExampleRules(
        templates=(EnvTemplateRule(path="a.env", required_keys=("SECRET",), removed_keys=("OLD",)),),
        private_key_marker="PRIVATE KEY",
        placeholder_marker="...",
    )
    problems = env_example.check_texts(rule, {"a.env": "OLD=1\nPRIVATE KEY\n"})
    assert any("残留已移除键 OLD" in item for item in problems)
    assert any("疑似真实私钥" in item for item in problems)
    assert any("缺键位 SECRET" in item for item in problems)


def test_system_fields_collection_unwrap() -> None:
    """清单常量包装形态：裸字面量 / 容器包装 / 无法识别。"""
    assert isinstance(system_fields._unwrap_collection(ast.parse("(1, 2)").body[0].value), ast.Tuple)  # type: ignore[attr-defined]
    wrapped = ast.parse("stable((rec(a='x'),))").body[0].value  # type: ignore[attr-defined]
    assert isinstance(system_fields._unwrap_collection(wrapped), ast.Tuple)
    assert system_fields._unwrap_collection(ast.parse("1").body[0].value) is None  # type: ignore[attr-defined]
    assert system_fields._unwrap_collection(ast.parse("f()").body[0].value) is None  # type: ignore[attr-defined]
    assert isinstance(system_fields._unwrap_collection(ast.parse("f(1, (2,))").body[0].value), ast.Tuple)  # type: ignore[attr-defined]


def test_system_fields_parse_variants(tmp_path: Path) -> None:
    """清单解析：赋值 / 标注赋值 / 元素非调用 / 枚举成员跳过 / kind 两种写法。"""
    write(
        tmp_path,
        {
            "src/forms.py": (
                "class FormKind(StrEnum):\n"
                "    DICT = 'dict'\n"
                "    NOT_STR = 1\n"
                "    @property\n"
                "    def helper(self):\n"
                "        return 1\n\n\n"
                "FORM_CATALOG = stable(\n"
                "    (\n"
                "        rec(form_key='alpha', table='sys_alpha', kind=FormKind.DICT.value),\n"
                "        rec(form_key='beta', table='sys_beta', kind='dict'),\n"
                "        rec(form_key=123, kind=FormKind.MISSING.value),\n"
                "        'not-a-call',\n"
                "    )\n"
                ")\n"
            ),
            "src/datasource.py": "DATA_SOURCE_CATALOG = (rec(data_source_key='alpha'),)\n",
        },
    )
    cfg = config(tmp_path)
    rule = cfg.system_fields_rules
    assert [record.get("form_key") for record in system_fields.parse_records(cfg, rule.catalog)][:3] == [
        "alpha",
        "beta",
        None,
    ]
    values = system_fields.parse_enum_values(cfg, rule.catalog)
    assert values == {"DICT": "dict"}
    declarations = system_fields.parse_kind_declarations(cfg, rule.catalog)
    assert declarations[0] == ("alpha", True, "dict")
    assert declarations[1] == ("beta", True, "dict")
    assert declarations[2] == ("?", True, None)
    assert system_fields.resolve_kind_node(ast.parse("'dict'").body[0].value, "FormKind", {}) == "dict"  # type: ignore[attr-defined]
    assert system_fields.resolve_kind_node(ast.parse("1").body[0].value, "FormKind", {}) is None  # type: ignore[attr-defined]
    empty = dataclasses.replace(rule.catalog, enum="")
    assert system_fields.parse_enum_values(cfg, empty) == {}
    assert system_fields.parse_enum_values(cfg, dataclasses.replace(rule.catalog, path="src/none.py")) == {}
    assert system_fields._sequence(tmp_path / "src/none.py", "FORM_CATALOG") == []

    write(
        tmp_path,
        {
            "src/annotated.py": (
                "class FormKind(StrEnum):\n    obj.attr = 'a'\n\n"
                "FORM_CATALOG: tuple = (rec(form_key='gamma', table='sys_gamma', kind=FormKind.A.value),)\n"
            )
        },
    )
    annotated = dataclasses.replace(rule.catalog, path="src/annotated.py")
    assert system_fields.parse_enum_values(cfg, annotated) == {}
    assert [record.get("form_key") for record in system_fields.parse_records(cfg, annotated)] == ["gamma"]


def test_system_fields_diff_functions() -> None:
    """纯函数：清单 ↔ 表文件双向、清单外表文件、数据源回填闭合、kind 显式声明。"""
    texts = {"sys_alpha": "| `form_key` | x |\n| `parent_id` | x |\n", "sys_extra": "| `form_key` | x |\n"}
    issues = system_fields.diff_form_catalog(
        [("alpha", "sys_alpha"), ("alpha", "sys_alpha"), ("Bad", "sys_beta"), ("gamma", "sys_gamma")],
        {**texts, "sys_gamma": "| `parent_id` | x |\n"},
        row_form_key="form_key",
        row_parent_id="parent_id",
        table_dir="表",
    )
    joined = "\n".join(issues)
    assert "格式非法" in joined
    assert "跨表重复" in joined
    assert "表名重复登记" in joined
    assert "清单表缺表文件" in joined
    assert "表文件缺 form_key 行" in joined
    assert "表文件有 form_key 行但未登记清单" in joined

    assert system_fields.diff_data_source_closure([("a", ""), ("b", "Bad"), ("c", "ghost")], {"alpha"}) == [
        "form_key 未回填 data_source_key：'a'",
        "data_source_key 格式非法：'b' → 'Bad'",
        "data_source_key 未登记：'c' → 'ghost'",
    ]
    assert system_fields.diff_explicit_kind([("a", False, None), ("b", True, "nope")], {"dict"}) == [
        "未显式声明 kind（现依赖数据类默认值）：'a'",
        "kind 取值非法或无法解析：'b' → 'nope'（须 dict）",
    ]


def test_system_fields_collect_branches(tmp_path: Path) -> None:
    """清单为空 / 数据源为空 / 枚举缺失 / 清单不可解析 / 表目录缺失。"""
    cfg = config(tmp_path)
    rule = cfg.system_fields_rules
    assert system_fields.load_table_texts(cfg, rule) == {}
    problems = system_fields.collect_problems(cfg, rule)
    assert any("清单不可解析或为空" in item for item in problems)

    write(
        tmp_path,
        {
            "src/forms.py": "FORM_CATALOG = (rec(form_key='alpha', table='sys_alpha'),)\n",
            "src/datasource.py": "DATA_SOURCE_CATALOG = ()\n",
        },
    )
    problems = system_fields.collect_problems(cfg, rule)
    assert any("数据源清单不可解析或为空" in item for item in problems)

    write(tmp_path, {"src/datasource.py": "DATA_SOURCE_CATALOG = (rec(data_source_key='alpha'),)\n"})
    problems = system_fields.collect_problems(cfg, rule)
    assert any("枚举不可解析" in item for item in problems)

    write(
        tmp_path,
        {"src/forms.py": "class FormKind(StrEnum):\n    DICT = 'dict'\n\nFORM_OTHER = ()\n"},
    )
    problems = system_fields.collect_problems(cfg, rule)
    assert any("清单不可解析或为空" in item for item in problems)

    write(
        tmp_path,
        {
            "src/forms.py": (
                "class FormKind(StrEnum):\n    DICT = 'dict'\n\n"
                "FORM_CATALOG = (rec(form_key='alpha', table='sys_alpha', kind=FormKind.DICT.value),)\n"
            ),
            "src/datasource.py": "DATA_SOURCE_CATALOG = (rec(data_source_key='alpha'),)\n",
        },
    )
    without_enum = dataclasses.replace(rule, datasource=dataclasses.replace(rule.datasource, enum=""))
    problems = system_fields.collect_problems(cfg, without_enum)
    assert not any("data_source_key] 枚举不可解析" in item for item in problems)


def test_system_fields_many_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """问题超过 50 项时只列前 50 并给出余数。"""
    entries = "".join(f"    rec(form_key='k{index}', table='t{index}'),\n" for index in range(51))
    write(
        tmp_path,
        {
            "src/forms.py": f"FORM_CATALOG = (\n{entries})\n",
            "src/datasource.py": "class DataSourceKind(StrEnum):\n    DICT = 'dict'\n\nDATA_SOURCE_CATALOG = ()\n",
        },
    )
    cfg = config(tmp_path)
    assert system_fields.run(cfg, as_self_test=False) == 1
    assert "其余" in capsys.readouterr().out


def test_config_design_naming_runtime(tmp_path: Path) -> None:
    """`[rules.base.design]` / `naming` / `runtime` 三条可选段的解析。"""
    extra = r"""
[rules.base.design]
dir = "docs/设计"
overview = "docs/总览.md"
table_subdir = "表"
dialect_subdir = "方言"
status_values = ["已定稿"]

[rules.base.naming]
tool_root = "scripts"
package_roots = ["frontend/packages"]
versions_root = "docs/版本"

[[rules.base.runtime]]
path = "src/codes.py"
extract = "code_pattern"
expect_count = 2
"""
    cfg = config(tmp_path, extra)
    base = cfg.base_rules
    assert base is not None
    assert base.design is not None and base.design.overview == "docs/总览.md"
    assert base.naming is not None and base.naming.tool_root == "scripts"
    assert len(base.runtime) == 1 and base.runtime[0].expect_count == 2


def _toml(**sections: str) -> str:
    """把片段拼成一份最小配置（`docs` 段必填）。

    Args:
        **sections: 段名 → 段文本。

    Returns:
        str: TOML 全文。
    """
    body = '[docs]\nroot = "docs"\n'
    return body + "".join(text for text in sections.values())


def test_config_type_errors(tmp_path: Path) -> None:
    """`reasons` 值非字符串 / `image_packages` 元素非字符串 / 模板清单为空 均报错。"""
    with pytest.raises(ConfigError, match="应为字符串"):
        parse(
            __import__("tomllib").loads(
                _toml(
                    reasons="[rules.bare_collections.reasons]\nbanned = 1\n[rules.bare_collections]\n"
                    'scan_targets = ["src"]\nwhitelist = []\nunregistered = []\nallow_marker = "#x"\n'
                    'baseline = "b.json"\n[rules.bare_collections.banned]\nbare = []\nreadonly = []\n'
                    "ascending = []\n"
                )
            ),
            tmp_path / "engkit.toml",
        )
    with pytest.raises(ConfigError, match="应为字符串数组"):
        parse(
            __import__("tomllib").loads(
                _toml(
                    boundaries="[rules.service_boundaries]\n"
                    'shared_groups = ["libs"]\nservice_groups = ["services"]\nshared_libs = ["core"]\n'
                    'service_packages = ["alpha"]\nruntime_packages = ["core"]\nlayout = "l.json"\n'
                    'exceptions = "e.json"\nbaseline = "b.json"\n'
                    'shared_models_marker = ""\ncounters = ["c"]\n'
                    "[rules.service_boundaries.layers.shared]\ncore = []\n"
                    "[rules.service_boundaries.layers.service]\nservices = []\n"
                    '[rules.service_boundaries.ownership]\nmodule = "m.py"\nconst = "T"\n'
                    'owner_every_service = "*"\ninfrastructure_tables = []\nexempt_modules = []\n'
                    '[rules.service_boundaries.catalog]\nmodule = "c.py"\nconst = "C"\n'
                    '[rules.service_boundaries.ops]\ndir = ""\ndockerfile = ""\nrun_image_values = []\n'
                    "[rules.service_boundaries.ops.image_packages]\nplatform = 1\n"
                )
            ),
            tmp_path / "engkit.toml",
        )
    with pytest.raises(ConfigError, match="至少一份模板"):
        parse(
            __import__("tomllib").loads(_toml(env="[rules.env_example]\nremoved_keys = []\ntemplates = []\n")),
            tmp_path / "engkit.toml",
        )


def test_config_runtime_extract_error(tmp_path: Path) -> None:
    """`[[rules.base.runtime]]` 的 `extract` 非法即报错。"""
    extra = '\n[[rules.base.runtime]]\npath = "src/codes.py"\nextract = "bad"\n'
    with pytest.raises(ConfigError, match="extract"):
        config(tmp_path, extra)


def test_metrics_urlopen_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """指标端点抛异常即降级标注（不中断采集）。"""
    from engkit.base import metrics

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("down")

    monkeypatch.setattr(metrics.urllib.request, "urlopen", boom)
    result = metrics.scrape_runtime("http://127.0.0.1:9/metrics", "m_total")
    assert result["available"] is False
    assert "指标端点不可达" in str(result["reason"])


def test_cli_main_without_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`main()` 缺省取 `sys.argv`（命令行真实入口分支）。"""
    cfg_path = tmp_path / "engkit.toml"
    cfg_path.write_text(RULES_TOML + '\n[docs]\nroot = "docs"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["engkit", "base", "env-example", "--config", str(cfg_path)])
    assert cli.main() == 2


# ---------- 原型复核 ----------

SAMPLE = (
    "## 4. 原型对照结论（实现 ↔ 原型）\n\n"
    "基准：[原型](原型/样例.html)。\n\n"
    "| # | 对照项 | 原型依据 | 实现实测 | 结论 |\n| --- | --- | --- | --- | --- |\n"
    "| 1 | 独立全屏 | 原型 | 无侧栏 | 通过 |\n\n"
    "| 截面 | 截图 |\n| --- | --- |\n"
    "| 实现 | [实现.png](资源/01_记录/实现.png) |\n"
)


def proto_fixture(tmp_path: Path) -> tuple[Config, Path]:
    """造原型复核夹具（原型文件 + 任务测试记录目录）。

    Args:
        tmp_path: 临时仓根。

    Returns:
        tuple[Config, Path]: （配置, 记录目录）。
    """
    record_dir = tmp_path / "docs/项目/01_阶段/任务/01_域/01_任务/测试"
    (record_dir / "资源/01_记录").mkdir(parents=True)
    (record_dir / "资源/01_记录/实现.png").write_bytes(b"png")
    write(
        tmp_path,
        {
            "docs/原型/样例.html": "<html></html>",
            "docs/项目/01_阶段/任务/01_域/01_任务/设计/01_设计.md": "原型：原型/样例.html\n",
        },
    )
    cfg = config(tmp_path)
    return dataclasses.replace(cfg, root=tmp_path), record_dir


def test_prototype_record_branches(tmp_path: Path) -> None:
    """记录校验：齐备通过 / 缺节 / 缺表头 / 无数据行 / 缺截图 / 不适用理由 / 原型文件缺失。"""
    cfg, record_dir = proto_fixture(tmp_path)
    rule = cfg.prototype_rules
    assert rule is not None
    record = record_dir / "01_记录.md"
    texts = [("record", SAMPLE)]
    record.write_text(SAMPLE, encoding="utf-8")
    assert prototype_review.check_records(cfg, [record], texts, rule) == []

    record.write_text("## 1. 测试信息\n\n无对照节\n", encoding="utf-8")
    assert any("缺「原型对照结论」节" in item for item in prototype_review.check_records(cfg, [record], texts, rule))

    record.write_text("## 4. 原型对照结论\n\n| 项 | 结论 |\n| --- | --- |\n", encoding="utf-8")
    problems = prototype_review.check_records(cfg, [record], [("record", "基准：原型/样例.html")], rule)
    assert any("无数据行" in item for item in problems)

    record.write_text("## 4. 原型对照结论\n\n无表\n", encoding="utf-8")
    problems = prototype_review.check_records(cfg, [record], [("record", "基准：原型/样例.html")], rule)
    assert any("缺表头含「结论」的对照表" in item for item in problems)

    record.write_text(SAMPLE.replace("[实现.png](资源/01_记录/实现.png)", ""), encoding="utf-8")
    problems = prototype_review.check_records(cfg, [record], [("record", "基准：原型/样例.html")], rule)
    assert any("缺**存在**的截图证据" in item for item in problems)

    record.write_text(SAMPLE.replace("资源/01_记录/实现.png", "资源/01_记录/缺.png"), encoding="utf-8")
    problems = prototype_review.check_records(cfg, [record], [("record", "基准：原型/样例.html")], rule)
    assert any("缺**存在**的截图证据" in item for item in problems)

    record.write_text("## 4. 原型对照结论\n\n| 项 | 值 |\n| --- | --- |\n| 1 | 2 |\n", encoding="utf-8")
    problems = prototype_review.check_records(cfg, [record], [("record", "基准：原型/样例.html")], rule)
    assert any("缺表头含「结论」的对照表" in item for item in problems)

    record.write_text("## 4. 原型对照结论\n\n不适用：界面无变更。\n", encoding="utf-8")
    problems = prototype_review.check_records(cfg, [record], [("record", "基准：原型/样例.html")], rule)
    assert any("未给理由" in item for item in problems)

    record.write_text(SAMPLE, encoding="utf-8")
    problems = prototype_review.check_records(cfg, [record], [("record", "原型/不存在.html")], rule)
    assert any("原型引用文件不存在" in item for item in problems)

    assert any(
        "未登记原型依据" in item for item in prototype_review.check_records(cfg, [record], [("record", "无")], rule)
    )


def test_read_section_and_task_texts(tmp_path: Path) -> None:
    """节截取（命中下一节即止）与任务上下文文本收集。"""
    text = "## 1. 甲\n\n内容\n\n## 2. 乙\n\n无原型\n"
    assert prototype_review.read_section(text, r"^##.*甲").strip() == "内容"
    assert prototype_review.read_section(text, r"^##.*丙") == ""
    root = tmp_path / "docs/项目/01_阶段/任务/01_域/01_任务"
    write(tmp_path, {"docs/项目/01_阶段/任务/01_域/01_任务/设计/01_设计.md": "x"})
    texts = prototype_review.task_texts(tmp_path, root, ("设计",))
    assert [name for name, _text in texts] == ["docs/项目/01_阶段/任务/01_域/01_任务/设计/01_设计.md"]


def test_prototype_changed_and_task_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """推送硬拦模式（含界面改动 + 记录 + 上下文）与单任务模式的正负分支。"""
    cfg, record_dir = proto_fixture(tmp_path)
    record = record_dir / "01_记录.md"
    record.write_text(SAMPLE, encoding="utf-8")
    rule = cfg.prototype_rules
    assert rule is not None
    task_rel = "docs/项目/01_阶段/任务/01_域/01_任务"
    changed = [
        "frontend/apps/desktop/src/views/a.vue",
        f"{task_rel}/测试/01_记录.md",
        f"{task_rel}/设计/01_设计.md",
    ]
    monkeypatch.setattr(prototype_review, "git_changed", lambda root, base: changed)
    assert prototype_review.run(cfg, changed="origin/main", task="", as_self_test=False) == 0

    record.write_text(SAMPLE.replace("| 通过 |", "| 待补 |"), encoding="utf-8")
    assert prototype_review.run(cfg, changed="origin/main", task="", as_self_test=False) == 1

    assert prototype_review.run(cfg, changed="", task=str(record_dir.parent), as_self_test=False) == 1
    record.write_text(SAMPLE, encoding="utf-8")
    assert prototype_review.run(cfg, changed="", task=str(record_dir.parent), as_self_test=False) == 0

    monkeypatch.setattr(prototype_review, "git_changed", lambda root, base: ["docs/readme.md"])
    assert prototype_review.run(cfg, changed="origin/main", task="", as_self_test=False) == 0
    capsys.readouterr()


def test_prototype_inspect_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """全仓巡检：缺节的记录逐条列出，超过 20 份给出余数提示。"""
    cfg, record_dir = proto_fixture(tmp_path)
    for index in range(21):
        (record_dir / f"{index:02d}_记录.md").write_text("## 1. 测试信息\n", encoding="utf-8")
    (record_dir / "99_齐备.md").write_text(SAMPLE, encoding="utf-8")
    assert prototype_review.inspect_mode(cfg, cfg.prototype_rules) == 0
    captured = capsys.readouterr().out
    assert "未落「原型对照结论」21 份" in captured
    assert "余 1 份" in captured


def test_prototype_git_changed_real(tmp_path: Path) -> None:
    """真实 git 仓库：变更清单可取；基准不可解析返回 None。"""
    repo = tmp_path
    write(repo, {"a.txt": "1\n"})
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": os.environ.get("PATH", ""),
    }
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "init")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    assert prototype_review.git_changed(repo, "HEAD") == []
    assert prototype_review.git_changed(repo, "no-such-ref") is None


def test_prototype_self_test_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """自检样例不达预期时退 1。"""
    cfg = config(tmp_path)
    rule = cfg.prototype_rules
    assert rule is not None
    monkeypatch.setattr(prototype_review, "check_records", lambda *args, **kwargs: ["x"])
    assert prototype_review.self_test(cfg, rule) == 1
    monkeypatch.setattr(prototype_review, "check_records", lambda *args, **kwargs: [])
    assert prototype_review.self_test(cfg, rule) == 1


def test_registry_spec_defaults() -> None:
    """登记源定位的缺省字段（枚举 / kind 字段可省）。"""
    spec = RegistrySpec(path="a.py", const="C", key_field="k")
    assert spec.enum == "" and spec.kind_field == "kind"
    assert json.dumps({"ok": True})


def test_system_fields_parse_enum_member_values(tmp_path: Path) -> None:
    """标识用枚举成员（`data_source_key=DataSourceKey.ROLE.value`）可解析；多枚举声明按名单限定。

    背景（2026-10-31 bms 02_01）：清单里的标识从字面量收敛为 `DataSourceKey` 枚举成员后，
    只认字符串字面量的解析会让清单被判为「不可解析或为空」，整条护栏失效。
    """
    write(
        tmp_path,
        {
            "src/forms.py": (
                "class FormKind(StrEnum):\n"
                "    DICT = 'dict'\n\n\n"
                "class DataSourceKey(StrEnum):\n"
                "    ROLE = 'role'\n\n\n"
                "FORM_CATALOG = (\n"
                "    rec(\n"
                "        form_key='alpha',\n"
                "        table='sys_alpha',\n"
                "        kind=FormKind.DICT.value,\n"
                "        data_source_key=DataSourceKey.ROLE.value,\n"
                "    ),\n"
                ")\n"
            )
        },
    )
    cfg = config(tmp_path)
    rule = cfg.system_fields_rules
    multi = dataclasses.replace(rule.catalog, enum="FormKind,DataSourceKey")
    records = system_fields.parse_records(cfg, multi)
    assert records == [{"form_key": "alpha", "table": "sys_alpha", "kind": "dict", "data_source_key": "role"}]
    assert system_fields.parse_enum_values(cfg, multi) == {"DICT": "dict", "ROLE": "role"}
    # 只声明 FormKind 时，别的枚举成员不解析（防止跨清单误配）
    only_form = dataclasses.replace(rule.catalog, enum="FormKind")
    assert "data_source_key" not in system_fields.parse_records(cfg, only_form)[0]
    node = ast.parse("DataSourceKey.ROLE.value").body[0].value  # type: ignore[attr-defined]
    assert system_fields.resolve_kind_node(node, "FormKind,DataSourceKey", {"ROLE": "role"}) == "role"
    assert system_fields.resolve_kind_node(node, "FormKind", {"ROLE": "role"}) is None
    # `**kwargs` 展开的关键字没有名称（arg=None），跳过而不是崩
    write(tmp_path, {"src/star.py": "FORM_CATALOG = (rec(form_key='beta', **extra),)\n"})
    star = dataclasses.replace(rule.catalog, path="src/star.py")
    assert system_fields.parse_records(cfg, star) == [{"form_key": "beta"}]


def test_system_fields_enum_declared_in_other_file(tmp_path: Path) -> None:
    """枚举定义在别处时用 `类名@路径` 声明（清单收敛到独立常量模块后的真实形态）。"""
    write(
        tmp_path,
        {
            "src/keys.py": "class DataSourceKey(StrEnum):\n    ROLE = 'role'\n",
            "src/registry.py": (
                "class DataSourceKind(StrEnum):\n"
                "    DICT = 'dict'\n\n\n"
                "DATA_SOURCE_CATALOG = (\n"
                "    rec(data_source_key=DataSourceKey.ROLE.value, kind=DataSourceKind.DICT.value),\n"
                ")\n"
            ),
        },
    )
    cfg = config(tmp_path)
    rule = cfg.system_fields_rules
    spec = dataclasses.replace(rule.datasource, path="src/registry.py", enum="DataSourceKind,DataSourceKey@src/keys.py")
    assert system_fields.parse_enum_values(cfg, spec) == {"ROLE": "role", "DICT": "dict"}
    assert system_fields.parse_records(cfg, spec) == [{"data_source_key": "role", "kind": "dict"}]
    # 未加 `@路径` 时只在清单同文件里找（找不到即空，不猜别处）
    only_local = dataclasses.replace(spec, enum="DataSourceKind,DataSourceKey")
    assert system_fields.parse_enum_values(cfg, only_local) == {"DICT": "dict"}
    assert system_fields.parse_records(cfg, only_local) == [{"kind": "dict"}]
    # 声明的枚举文件不存在：跳过而不崩
    missing = dataclasses.replace(spec, enum="DataSourceKey@src/none.py")
    assert system_fields.parse_enum_values(cfg, missing) == {}
    # 空项与「只有路径没类名」的项被忽略（容错声明写法）
    assert system_fields.enum_names_of("A@x.py, B ,C@y.py, ,@orphan.py") == frozenset({"A", "B", "C"})
