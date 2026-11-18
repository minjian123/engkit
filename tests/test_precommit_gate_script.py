"""提交前门禁脚本的裁剪用例（pyright 按项目声明的检查范围收敛）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tools" / "preflight" / "提交前预检.py"
"""被测脚本（仓内工具，不是包模块，按文件加载）。"""


def _load_gate():
    """按文件路径加载门禁脚本。

    Returns:
        Any: 已执行初始化的模块对象。
    """
    spec = importlib.util.spec_from_file_location("engkit_precommit_gate", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    """门禁脚本模块（模块级缓存，避免每个用例重复执行脚本顶层）。"""
    return _load_gate()


def test_pyright_scope_reads_include(gate, tmp_path: Path) -> None:
    """只在 `[tool.pyright].include` 声明时收敛；缺文件 / 缺段 / 坏 TOML 一律不收敛。"""
    assert gate.pyright_scope(tmp_path) == ()
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
    assert gate.pyright_scope(tmp_path) == ()
    (tmp_path / "pyproject.toml").write_text('[tool.pyright]\ntypeCheckingMode = "strict"\n', encoding="utf-8")
    assert gate.pyright_scope(tmp_path) == ()
    (tmp_path / "pyproject.toml").write_text('[tool.pyright]\ninclude = ["src", "ops/"]\n', encoding="utf-8")
    assert gate.pyright_scope(tmp_path) == ("src", "ops/")
    (tmp_path / "pyproject.toml").write_text("[tool.pyright\n", encoding="utf-8")
    assert gate.pyright_scope(tmp_path) == ()


def test_within_scope_matches_by_path_segment(gate) -> None:
    """前缀按路径段匹配（`srcx/` 不算命中 `src`），`.` 表示整树放行。"""
    assert gate.within_scope("src/engkit/cli.py", ("src",)) is True
    assert gate.within_scope("src/engkit/cli.py", ("src/",)) is True
    assert gate.within_scope("tests/test_cli.py", ("src",)) is False
    assert gate.within_scope("srcx/mod.py", ("src",)) is False
    assert gate.within_scope("anything.py", (".",)) is True
    assert gate.within_scope("anything.py", ()) is False


def test_step_argv_drops_files_outside_pyright_scope(gate, tmp_path: Path, monkeypatch) -> None:
    """pyright 步骤只拿 `include` 范围内的改动文件；只剩范围外文件时整步跳过。"""
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_cfg", lambda: None)
    monkeypatch.setattr("engkit.preflight.expand", lambda cfg, tokens: list(tokens))
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyright]\ninclude = ["src"]\n',
        encoding="utf-8",
    )
    step = SimpleNamespace(cmd=("python", "-m", "pyright", "--outputjson"))
    argv = gate.step_argv(step, tmp_path, ["src/engkit/cli.py", "tests/test_cli.py"], scoped=True)
    assert argv == ["python", "-m", "pyright", "--outputjson", "src/engkit/cli.py"]
    assert gate.step_argv(step, tmp_path, ["tests/test_cli.py"], scoped=True) is None
    # 未声明 include 时保持原样（不擅自放宽，也不擅自收紧）
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    assert gate.step_argv(step, tmp_path, ["tests/test_cli.py"], scoped=True) == [
        "python",
        "-m",
        "pyright",
        "--outputjson",
        "tests/test_cli.py",
    ]


def test_step_argv_keeps_ruff_scope(gate, tmp_path: Path, monkeypatch) -> None:
    """`ruff` 步骤不受 pyright 范围影响：测试文件照样交给 ruff。"""
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_cfg", lambda: None)
    monkeypatch.setattr("engkit.preflight.expand", lambda cfg, tokens: list(tokens))
    (tmp_path / "pyproject.toml").write_text('[tool.pyright]\ninclude = ["src"]\n', encoding="utf-8")
    step = SimpleNamespace(cmd=("ruff", "check", "."))
    argv = gate.step_argv(step, tmp_path, ["src/engkit/cli.py", "tests/test_cli.py"], scoped=True)
    assert argv == ["ruff", "check", "src/engkit/cli.py", "tests/test_cli.py"]
