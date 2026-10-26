"""工具链基线护栏（`13_07`，Kiwi 2360）：基线单一来源 / 各仓只 extend / 版本随 devtools 下发。

三条断言对应任务完成标准：`toolchain/ruff-base.toml` 是 ruff 规则的唯一来源；接入仓配置
只写 `extend` + 本仓专属项；ruff / pyright 版本由 `[project.optional-dependencies].devtools` 锁定。
受检仓配置清单来自仓配置 `[toolchain].ruff_configs`（包内不出现任何仓名字面）。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from engkit.config import ConfigError, load

ROOT = Path(__file__).resolve().parents[1]
"""engkit 仓根。"""

KIWI_ID = 2360
"""本条任务的用例编号（多条断言共用同一编号）。"""

FORBIDDEN_TOOL_KEYS = ("line-length", "select", "ignore")
"""不得出现在接入仓 `[tool.ruff]` 的规则键（基线内容，复制即口径分叉）。"""

FORBIDDEN_LINT_KEYS = ("select", "ignore", "fixable", "unfixable")
"""不得出现在接入仓 `[tool.ruff.lint]` 的规则键（同上）。"""

TOOL_DEPS = ("ruff", "pyright")
"""由 devtools 组下发的工具（版本不得在仓内另行声明）。"""


def _read_toml(path: Path) -> dict[str, Any]:
    """读取 TOML 文件。

    Args:
        path: 文件路径。

    Returns:
        dict[str, Any]: 解析结果。
    """
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _ruff_violations(config_path: Path) -> list[str]:
    """检查一份仓配置的 `[tool.ruff]` 段是否「只 extend、不复制规则段」。

    Args:
        config_path: 仓配置文件路径（`pyproject.toml`）。

    Returns:
        list[str]: 违规说明清单（空表示合规）。
    """
    data = _read_toml(config_path)
    tool = data.get("tool", {}).get("ruff", {})
    if not tool:
        return ["缺少 [tool.ruff] 段"]
    problems: list[str] = []
    extend = tool.get("extend")
    if not extend:
        problems.append("缺少 extend（规则须指向 engkit 基线）")
    else:
        target = (config_path.parent / extend).resolve()
        if not target.is_file():
            problems.append(f"extend 目标不可解析：{extend}")
    problems.extend(f"复制了基线键 {key}" for key in FORBIDDEN_TOOL_KEYS if key in tool)
    lint = tool.get("lint", {})
    problems.extend(f"复制了基线键 lint.{key}" for key in FORBIDDEN_LINT_KEYS if key in lint)
    return problems


@pytest.mark.kiwi_id(KIWI_ID)
def test_toolchain_baseline_only_shared_keys() -> None:
    """基线存在且只含共用项：三键齐备，本仓专属项不得混入。"""
    cfg = load(ROOT / "engkit.toml")
    baseline = cfg.resolve(cfg.toolchain.baseline)
    assert baseline.is_file(), f"基线缺失：{baseline}"
    data = _read_toml(baseline)
    assert data["line-length"] == 120
    lint = data["lint"]
    assert set(lint["select"]) == {"E", "F", "W", "I", "B", "UP", "SIM", "RUF"}
    assert set(lint["ignore"]) == {"RUF001", "RUF002", "RUF003"}
    # 本仓专属项（各仓 Python 版本 / 包结构不同）不得写进基线
    assert "target-version" not in data
    assert "known-first-party" not in str(data)
    assert "format" not in data


@pytest.mark.kiwi_id(KIWI_ID)
def test_repo_configs_only_extend() -> None:
    """各接入仓配置只 extend 基线、不复制规则段；extend 目标必须可解析。"""
    cfg = load(ROOT / "engkit.toml")
    registered = cfg.toolchain.ruff_configs
    assert registered, "[toolchain].ruff_configs 为空：护栏会空转"
    checked = 0
    problems: list[str] = []
    for rel in registered:
        config_path = (cfg.root / rel).resolve()
        if not config_path.is_file():
            # 单仓克隆（CI）场景：对端仓不存在即跳过，不误判
            continue
        checked += 1
        problems.extend(f"{rel}：{item}" for item in _ruff_violations(config_path))
    assert checked > 0, "未校验任何仓配置：登记路径全部不存在"
    assert problems == []


@pytest.mark.parametrize(
    "snippet",
    (
        '[tool.ruff]\nextend = "base.toml"\nline-length = 120\n',
        '[tool.ruff]\nextend = "base.toml"\n[tool.ruff.lint]\nselect = ["E"]\n',
        "[tool.ruff]\nline-length = 120\n",
        '[tool.ruff]\nextend = "missing.toml"\n',
    ),
)
def test_ruff_violations_detect_copies(tmp_path: Path, snippet: str) -> None:
    """护栏函数本身有效（防清单被清空 / 判定被写坏成空转）。"""
    (tmp_path / "base.toml").write_text("line-length = 120\n", encoding="utf-8")
    config = tmp_path / "pyproject.toml"
    config.write_text(snippet, encoding="utf-8")
    assert _ruff_violations(config)


def test_ruff_violations_accept_compliant_config(tmp_path: Path) -> None:
    """合规配置（只 extend + 本仓专属项）零违规。"""
    (tmp_path / "base.toml").write_text("line-length = 120\n", encoding="utf-8")
    config = tmp_path / "pyproject.toml"
    config.write_text(
        '[tool.ruff]\nextend = "base.toml"\ntarget-version = "py314"\n',
        encoding="utf-8",
    )
    assert _ruff_violations(config) == []


@pytest.mark.kiwi_id(KIWI_ID)
def test_devtools_versions_declared() -> None:
    """devtools 组声明 ruff 与 pyright 的版本下限，且仓内 dev 组不重复声明。"""
    data = _read_toml(ROOT / "pyproject.toml")
    devtools = data["project"]["optional-dependencies"]["devtools"]
    for tool in TOOL_DEPS:
        declared = [item for item in devtools if item.split(">")[0].split("=")[0].split("~")[0] == tool]
        assert declared, f"devtools 未声明 {tool}"
        assert any(spec in declared[0] for spec in (">=", "==", "~=")), f"{tool} 未声明版本下限：{declared[0]}"
    dev_group = data["dependency-groups"]["dev"]
    offenders = [item for item in dev_group if item.split(">")[0].split("=")[0].strip() in TOOL_DEPS]
    assert offenders == [], f"dev 组重复声明工具版本：{offenders}"


def test_toolchain_section_parsed(build_repo) -> None:
    """`[toolchain]` 段可解析（登记项来自配置，包内不写死仓名）。"""
    root = build_repo(
        config='[docs]\nroot = "docs"\n[toolchain]\nbaseline = "toolchain/base.toml"\n'
        'ruff_configs = ["pyproject.toml", "../other/pyproject.toml"]\n'
    )
    cfg = load(root / "engkit.toml")
    assert cfg.toolchain.baseline == "toolchain/base.toml"
    assert cfg.toolchain.ruff_configs == ("pyproject.toml", "../other/pyproject.toml")


def test_toolchain_section_defaults(repo: Path) -> None:
    """缺段取缺省值（未接入工具链基线的仓不受影响）。"""
    cfg = load(repo / "engkit.toml")
    assert cfg.toolchain.baseline == "toolchain/ruff-base.toml"
    assert cfg.toolchain.ruff_configs == ()


def test_toolchain_unknown_key_rejected(build_repo) -> None:
    """未知键即报错（配置非法不得靠缺省值掩盖）。"""
    root = build_repo(config='[docs]\nroot = "docs"\n[toolchain]\nbaslines = "x"\n')
    with pytest.raises(ConfigError, match="toolchain"):
        load(root / "engkit.toml")
