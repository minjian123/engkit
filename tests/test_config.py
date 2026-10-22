"""配置解析与校验用例。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from engkit.config import (
    ConfigError,
    discover,
    find_upward,
    load,
    load_for,
    parse,
)


def _write(tmp_path: Path, body: str, name: str = "engkit.toml") -> Path:
    """写一份配置并返回路径。

    Args:
        tmp_path: 临时目录。
        body: 配置正文。
        name: 文件名。

    Returns:
        Path: 配置路径。
    """
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_full_config(cfg) -> None:
    """完整配置的各段均被解析。"""
    assert cfg.name == "mini"
    assert cfg.docs.root == "docs"
    assert cfg.docs.skip == ("docs/用户文档/",)
    assert cfg.docs.scope.whitelist_dirs == ("规范/",)
    assert cfg.backend is not None
    assert cfg.backend.projects == ("libs/pkg_a", "services/svc")
    assert cfg.frontend is not None
    assert cfg.frontend.workspace_protocol == "workspace:"
    assert cfg.coverage.plugin == "plug"
    assert cfg.pytest.max_rounds == 2
    assert cfg.backend_global.exact == ("backend/pyproject.toml",)
    assert [rule.match_prefix for rule in cfg.backend_extra] == ["backend/ops/", "scripts/tools/", "scripts/check/"]
    assert cfg.frontend_global.prefixes == ("scripts/",)
    assert cfg.guard.scripts == ("check-preflight.py",)
    assert cfg.kiwi.container == "mini-kiwi"
    assert cfg.runonce.window_seconds == 1800
    assert cfg.preflight.steps[0].label == "静态：echo"


def test_config_paths(cfg, repo: Path) -> None:
    """路径解析与属性。"""
    assert cfg.resolve("docs") == repo / "docs"
    assert cfg.docs_root == repo / "docs"
    assert cfg.backend_root == repo / "backend"
    assert cfg.frontend_root == repo / "frontend"


def test_config_without_optional_sections(tmp_path: Path) -> None:
    """只给必需段时取缺省值，后端 / 前端为 None。"""
    path = _write(tmp_path, '[docs]\nroot = "doc"\n')
    cfg = load(path)
    assert cfg.name == tmp_path.name
    assert cfg.backend is None and cfg.backend_root is None
    assert cfg.frontend is None and cfg.frontend_root is None
    assert cfg.coverage.fail_under == 100.0
    assert cfg.backend_extra == ()
    assert cfg.guard.dir_test_threshold == 20


def test_missing_docs_section(tmp_path: Path) -> None:
    """缺少 `[docs]` 报错。"""
    path = _write(tmp_path, "[repo]\nname = 'x'\n")
    with pytest.raises(ConfigError, match="docs"):
        load(path)


def test_missing_required_key(tmp_path: Path) -> None:
    """`[docs].root` 必填。"""
    path = _write(tmp_path, "[docs]\nskip = []\n")
    with pytest.raises(ConfigError, match="root"):
        load(path)


def test_unknown_top_key(tmp_path: Path) -> None:
    """未知顶层键报错。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[nope]\nx = 1\n')
    with pytest.raises(ConfigError, match="未知键"):
        load(path)


def test_section_must_be_table(tmp_path: Path) -> None:
    """段类型不是表时报错。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n')
    raw = {"docs": "not-a-table"}
    with pytest.raises(ConfigError, match="必须是表"):
        parse(raw, path)


def test_string_type_error(tmp_path: Path) -> None:
    """字符串键类型不符报错。"""
    path = _write(tmp_path, "[docs]\nroot = 1\n")
    with pytest.raises(ConfigError, match="类型应为 str"):
        load(path)


def test_int_type_rejects_bool(tmp_path: Path) -> None:
    """整数键不接受布尔值。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[selection]\nbase_threshold = true\n')
    with pytest.raises(ConfigError, match="类型应为 int"):
        load(path)


def test_strings_type_error(tmp_path: Path) -> None:
    """字符串数组类型不符报错。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\nskip = [1]\n')
    with pytest.raises(ConfigError, match="字符串数组"):
        load(path)


def test_tables_type_error(tmp_path: Path) -> None:
    """表数组类型不符报错。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[preflight]\nsteps = "x"\n')
    with pytest.raises(ConfigError, match="表数组"):
        load(path)


def test_budget_type_error(tmp_path: Path) -> None:
    """预算数组类型不符报错。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[preflight.budget]\nscoped = [1]\n')
    with pytest.raises(ConfigError, match="单步秒数"):
        load(path)


def test_budget_custom(tmp_path: Path) -> None:
    """预算可覆盖。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[preflight.budget]\nscoped = [10, 20]\nfull = [30.5, 40]\n')
    cfg = load(path)
    assert cfg.preflight.budget.scoped == (10.0, 20.0)
    assert cfg.preflight.budget.full == (30.5, 40.0)


def test_step_type_invalid(tmp_path: Path) -> None:
    """步骤类型非法报错。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[[preflight.steps]]\nlabel = "x"\ntype = "nope"\n')
    with pytest.raises(ConfigError, match="type"):
        load(path)


def test_step_preset_invalid(tmp_path: Path) -> None:
    """步骤档位非法报错。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[[preflight.steps]]\nlabel = "x"\ncmd = ["a"]\npreset = "nope"\n')
    with pytest.raises(ConfigError, match="preset"):
        load(path)


def test_step_command_requires_cmd(tmp_path: Path) -> None:
    """`command` 类型必须给 cmd。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[[preflight.steps]]\nlabel = "x"\n')
    with pytest.raises(ConfigError, match="cmd"):
        load(path)


def test_extra_rule_requires_matcher(tmp_path: Path) -> None:
    """特殊路径规则必须给出匹配方式。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[[rules.backend.extra_tests]]\nmatch_prefix = "x/"\n')
    with pytest.raises(ConfigError, match="test_dir_suffix"):
        load(path)


def test_extra_rule_unknown_key(tmp_path: Path) -> None:
    """特殊路径规则未知键报错。"""
    path = _write(
        tmp_path,
        '[docs]\nroot = "d"\n[[rules.backend.extra_tests]]\nmatch_prefix = "x/"\ntest_files = []\nbogus = 1\n',
    )
    with pytest.raises(ConfigError, match="未知键"):
        load(path)


def test_scope_extra_rules(tmp_path: Path) -> None:
    """`[docs.scope].extra_rules` 追加规则。"""
    path = _write(
        tmp_path,
        '[docs]\nroot = "d"\n[[docs.scope.extra_rules]]\npattern = "死线"\nlabel = "死线字样"\n',
    )
    cfg = load(path)
    assert cfg.docs.scope.extra_rules[0].label == "死线字样"


def test_scope_extra_rule_unknown_key(tmp_path: Path) -> None:
    """`extra_rules` 未知键报错。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[[docs.scope.extra_rules]]\npattern = "x"\nbad = 1\n')
    with pytest.raises(ConfigError, match="未知键"):
        load(path)


def test_mapping_unknown_key(tmp_path: Path) -> None:
    """mapping 条目未知键报错。"""
    path = _write(
        tmp_path,
        '[docs]\nroot = "d"\n[[rules.backend.extra_tests]]\nmatch_prefix = "x/"\n'
        'mapping = [{ script = "a", test = "b", bad = 1 }]\n',
    )
    with pytest.raises(ConfigError, match="未知键"):
        load(path)


def test_load_missing_file(tmp_path: Path) -> None:
    """配置文件不存在报错。"""
    with pytest.raises(ConfigError, match="读取配置失败"):
        load(tmp_path / "nope.toml")


def test_load_bad_toml(tmp_path: Path) -> None:
    """TOML 语法错误报错。"""
    path = _write(tmp_path, "[docs\nroot='x'\n")
    with pytest.raises(ConfigError, match="解析失败"):
        load(path)


def test_discover_and_load_for(repo: Path) -> None:
    """按仓根查找并加载配置；`--config` 覆盖路径。"""
    assert discover(repo) == repo / "engkit.toml"
    assert discover(repo / "docs") is None
    assert load_for(repo).name == "mini"
    assert load_for(repo, str(repo / "engkit.toml")).name == "mini"


def test_load_for_missing(tmp_path: Path) -> None:
    """仓根无配置时报错并给出指引。"""
    with pytest.raises(ConfigError, match=r"未找到 engkit\.toml"):
        load_for(tmp_path)


def test_find_upward(repo: Path, tmp_path: Path) -> None:
    """自子目录向上找到配置。"""
    assert find_upward(repo / "docs" / "规范") == repo / "engkit.toml"
    outside = tmp_path / "outside"
    outside.mkdir()
    assert find_upward(outside, limit=0) is None


def test_self_tests_and_steps_parsed(cfg) -> None:
    """自测与步骤字段。"""
    assert cfg.preflight.self_tests[0].deps == ("scripts/tools/guard.py",)
    assert cfg.preflight.steps[1].preset == "fast"
    assert cfg.preflight.steps[2].optional is True
    assert cfg.preflight.since == "origin/main"


def test_config_error_message_location(tmp_path: Path) -> None:
    """错误信息带位置。"""
    path = _write(tmp_path, '[docs]\nroot = "d"\n[kiwi]\nbogus = 1\n')
    with pytest.raises(ConfigError, match="配置 kiwi：未知键"):
        load(path)


def test_root_derived_from_config_dir(tmp_path: Path) -> None:
    """仓根取配置文件所在目录（不依赖其他文件）。"""
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    path = nested / "engkit.toml"
    path.write_text('[docs]\nroot = "docs"\n', encoding="utf-8")
    assert load(path).root == nested.resolve()


def test_base_config_parses(build_repo) -> None:
    """缺省夹具配置可解析（防夹具漂移）。"""
    root = build_repo()
    assert load(root / "engkit.toml").backend is not None
