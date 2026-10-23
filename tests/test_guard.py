"""重命令拦截用例。"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from engkit import guard
from engkit.config import GuardConfig

GUARD = GuardConfig(scripts=("check-preflight.py",), repo_dirs=("libs", "services"), dir_test_threshold=1)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """清掉所有相关环境变量，避免环境污染。"""
    for name in (
        *guard.CI_ENVS,
        guard.ALLOW_ENV,
        guard.FULL_ENV,
        guard.HOOK_ENV,
        guard.HOOK_SKIP_ENV,
        guard._CONFIG_ENV,
        "ENGKIT_RUN_ONCE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(guard, "_checked", False)


def test_is_ci(monkeypatch) -> None:
    """CI 标识判定。"""
    assert guard.is_ci() is False
    monkeypatch.setenv("GITLAB_CI", "true")
    assert guard.is_ci() is True


def test_is_wrapped(monkeypatch) -> None:
    """包装标记判定。"""
    assert guard.is_wrapped() is False
    monkeypatch.setenv("ENGKIT_RUN_ONCE", "1")
    assert guard.is_wrapped() is True


def test_allow_flags(monkeypatch) -> None:
    """显式绕过 / 显式全量的真值判定。"""
    assert guard.allow_unwrapped() is False
    monkeypatch.setenv(guard.ALLOW_ENV, "yes")
    assert guard.allow_unwrapped() is True
    assert guard.allow_full() is False
    monkeypatch.setenv(guard.FULL_ENV, "true")
    assert guard.allow_full() is True


def test_positional() -> None:
    """位置参数跳过选项、选项取值与子命令。"""
    args = ["check", "libs", "-k", "expr", "--deselect", "x", "-q", "--cov", "m"]
    assert guard.positional(args) == ["libs"]


def test_count_test_files(tmp_path: Path) -> None:
    """测试文件计数（含 `*_test.py`）。"""
    (tmp_path / "test_a.py").write_text("", encoding="utf-8")
    (tmp_path / "a_test.py").write_text("", encoding="utf-8")
    (tmp_path / "other.py").write_text("", encoding="utf-8")
    assert guard.count_test_files(tmp_path) == 2
    assert guard.count_test_files(tmp_path / "other.py") == 0


def test_classify(tmp_path: Path) -> None:
    """重命令分类。"""
    assert guard.classify([], GUARD) == ""
    assert guard.classify(["python", "scripts/check-preflight.py"], GUARD) == "script"
    assert guard.classify(["pytest"], GUARD) == "pytest"
    assert guard.classify(["python", "-m", "pytest"], GUARD) == "pytest"
    assert guard.classify(["pytest", "tests/test_a.py"], GUARD) == ""
    assert guard.classify(["pytest", "tests/test_a.py::test_x"], GUARD) == ""
    big = tmp_path / "many"
    big.mkdir()
    for index in range(3):
        (big / f"test_{index}.py").write_text("", encoding="utf-8")
    assert guard.classify(["pytest", str(big)], GUARD) == "pytest"
    assert guard.classify(["pyright"], GUARD) == "pyright"
    assert guard.classify(["pyright", "a.py"], GUARD) == ""
    assert guard.classify(["ruff", "check", "libs"], GUARD) == "ruff"
    assert guard.classify(["ruff", "check", "./"], GUARD) == "ruff"
    assert guard.classify(["ruff", "check", "a.py"], GUARD) == ""
    assert guard.classify(["mytool", "x"], GUARD) == ""


def test_should_block(monkeypatch) -> None:
    """拦截判定（CI / 已包装 / 显式绕过放行）。"""
    assert guard.should_block(["pytest"], GUARD) == "pytest"
    monkeypatch.setenv("CI", "1")
    assert guard.should_block(["pytest"], GUARD) == ""


def test_has_explicit_scope() -> None:
    """范围声明判定。"""
    assert guard.has_explicit_scope([]) is False
    assert guard.has_explicit_scope(["-q"]) is False
    assert guard.has_explicit_scope(["-k", "x"]) is True
    assert guard.has_explicit_scope(["-k=x"]) is True
    assert guard.has_explicit_scope(["tests/test_a.py"]) is True


def test_full_test_should_block(monkeypatch) -> None:
    """全量测试拦截判定。"""
    assert guard.full_test_should_block([]) is True
    assert guard.full_test_should_block(["--full"]) is False
    assert guard.full_test_should_block(["tests/test_a.py"]) is False
    monkeypatch.setenv("CI", "1")
    assert guard.full_test_should_block([]) is False


def test_guidance_texts() -> None:
    """提示文案包含包装命令与逃生开关。"""
    heavy = guard.heavy_guidance("pytest")
    assert "engkit run-once" in heavy
    assert guard.ALLOW_ENV in heavy
    full = guard.full_test_guidance()
    assert "engkit run-selected" in full
    assert guard.FULL_ENV in full


def test_pytest_addoption() -> None:
    """注册 `--full` 开关。"""
    recorded: list[tuple] = []

    class _Parser:
        def addoption(self, *args, **kwargs):
            recorded.append((args, kwargs))

    guard.pytest_addoption(_Parser())
    assert recorded and recorded[0][0][0] == "--full"


def _config(args: list[str]) -> types.SimpleNamespace:
    """构造 pytest 配置替身。

    Args:
        args: 命令行参数。

    Returns:
        types.SimpleNamespace: 替身对象。
    """
    return types.SimpleNamespace(invocation_params=types.SimpleNamespace(args=args))


def test_pytest_configure_heavy(monkeypatch, cfg) -> None:
    """命中重命令即 `UsageError`。"""
    monkeypatch.setenv(guard._CONFIG_ENV, str(cfg.path))
    with pytest.raises(Exception, match="重命令守卫"):
        guard.pytest_configure(_config([]))
    assert guard.pytest_configure(_config(["tests/test_a.py"])) is None


def test_pytest_configure_full(monkeypatch, cfg) -> None:
    """未声明全量即 `UsageError`。"""
    monkeypatch.setenv(guard._CONFIG_ENV, str(cfg.path))
    monkeypatch.setattr(guard, "classify", lambda argv, config: "")
    with pytest.raises(Exception, match="全量测试守卫"):
        guard.pytest_configure(_config([]))


def test_pytest_configure_ci(monkeypatch) -> None:
    """CI 环境放行（配置缺失也放行）。"""
    monkeypatch.setenv("CI", "1")
    assert guard.pytest_configure(_config([])) is None


def test_config_for_hook(monkeypatch, cfg, repo: Path) -> None:
    """配置定位：显式路径 / 自当前目录上溯 / 定位失败。"""
    monkeypatch.setenv(guard._CONFIG_ENV, str(cfg.path))
    assert guard.config_for_hook() is not None
    monkeypatch.setenv(guard._CONFIG_ENV, str(repo / "missing.toml"))
    assert guard.config_for_hook() is None
    monkeypatch.delenv(guard._CONFIG_ENV)
    monkeypatch.chdir(repo / "docs")
    found = guard.config_for_hook()
    assert found is not None and found.name == "mini"


def test_site_packages(tmp_path: Path) -> None:
    """venv 布局定位（Windows / POSIX / 缺失）。"""
    windows = tmp_path / "win" / "Lib" / "site-packages"
    windows.mkdir(parents=True)
    assert guard.site_packages(tmp_path / "win") == windows
    posix = tmp_path / "posix" / "lib" / "python3.11" / "site-packages"
    posix.mkdir(parents=True)
    assert guard.site_packages(tmp_path / "posix") == posix
    assert guard.site_packages(tmp_path / "none") is None


def test_hook_lifecycle(tmp_path: Path) -> None:
    """`.pth` 安装 / 校验 / 卸载。"""
    site = tmp_path / "site-packages"
    site.mkdir()
    source = tmp_path / "src"
    assert guard.check_hook(site, source) == 1
    target = guard.install_hook(site, source)
    assert target.is_file()
    assert guard.check_hook(site, source) == 0
    assert "过期" in _check_output(site, tmp_path / "other")
    assert guard.remove_hook(site) == 0
    assert guard.remove_hook(site) == 0


def _check_output(site: Path, source: Path) -> str:
    """执行校验并返回打印内容。

    Args:
        site: site-packages。
        source: 源码根。

    Returns:
        str: 打印内容。
    """
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert guard.check_hook(site, source) == 1
    return buffer.getvalue()


def test_render_pth(tmp_path: Path) -> None:
    """`.pth` 内容包含源码根与 `boot()` 调用。"""
    text = guard.render_pth(tmp_path)
    assert "sys.path.insert" in text
    assert "_engkit_guard.boot()" in text


def test_check_variants(monkeypatch, cfg, repo: Path, tmp_path: Path) -> None:
    """钩子检查：内部标记跳过 / 无配置放过 / 命中退出 / 未命中放过。"""
    monkeypatch.setenv(guard.HOOK_ENV, "1")
    assert guard.check(["pytest"]) is None
    monkeypatch.delenv(guard.HOOK_ENV)

    monkeypatch.setenv(guard.HOOK_SKIP_ENV, "1")
    assert guard.check(["pytest"]) is None
    monkeypatch.delenv(guard.HOOK_SKIP_ENV)

    monkeypatch.setattr(guard, "find_upward", lambda start, limit=6: None)
    assert guard.check(["pytest"]) is None

    monkeypatch.setenv(guard._CONFIG_ENV, str(cfg.path))
    with pytest.raises(SystemExit):
        guard.check(["pytest"])
    monkeypatch.setenv("CI", "1")
    assert guard.check(["pytest"]) is None


def test_boot(monkeypatch, cfg) -> None:
    """钩子硬退出与放行两条路径。"""
    exits: list[int] = []
    monkeypatch.setenv("CI", "1")
    guard.boot(exits.append)
    assert exits == []
    monkeypatch.delenv("CI")
    monkeypatch.setenv(guard._CONFIG_ENV, str(cfg.path))
    monkeypatch.setattr("sys.argv", ["pytest"])
    guard.boot(exits.append)
    assert exits == [1]
