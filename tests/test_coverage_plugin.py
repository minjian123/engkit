"""通用覆盖率采集插件用例（`engkit.coverage_plugin`）。"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

from engkit import coverage_plugin, guard


class _FakeCoverage:
    """假的 coverage 实例（记录调用，不真采集）。"""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        self.saved = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def save(self) -> None:
        self.saved += 1


def _fake_module(store: list[_FakeCoverage]) -> types.ModuleType:
    """造一个只提供 `Coverage` 的假 coverage 模块。

    Args:
        store: 创建出的实例清单（供断言）。

    Returns:
        types.ModuleType: 假模块。
    """

    def factory(**kwargs) -> _FakeCoverage:
        instance = _FakeCoverage(**kwargs)
        store.append(instance)
        return instance

    module = types.ModuleType("coverage")
    module.Coverage = factory  # type: ignore[attr-defined]
    return module


def test_start_without_config(monkeypatch, tmp_path: Path) -> None:
    """定位不到配置时静默不采。"""
    monkeypatch.delenv(guard._CONFIG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert coverage_plugin.start() is None


def test_start_without_data_env(monkeypatch, repo: Path) -> None:
    """配置存在但未注入采集环境变量时不采。"""
    monkeypatch.chdir(repo)
    monkeypatch.delenv("ENGKIT_COVERAGE_DATA", raising=False)
    assert coverage_plugin.start() is None


def test_start_collects(monkeypatch, repo: Path, tmp_path: Path) -> None:
    """注入采集变量后按进程号与采集范围启动（行 + 分支）。"""
    store: list[_FakeCoverage] = []
    monkeypatch.setitem(sys.modules, "coverage", _fake_module(store))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("ENGKIT_COVERAGE_DATA", str(tmp_path / ".coverage"))
    monkeypatch.setenv("ENGKIT_COVERAGE_SOURCE", "pkg_a, svc")
    instance = coverage_plugin.start()
    assert instance is not None and instance.started == 1
    assert instance.kwargs["data_suffix"] == f"{os.getpid()}"
    assert instance.kwargs["branch"] is True
    assert instance.kwargs["source"] == ("pkg_a", "svc")


def test_start_without_source(monkeypatch, repo: Path, tmp_path: Path) -> None:
    """未声明采集范围时不按包过滤（`source=None`）。"""
    store: list[_FakeCoverage] = []
    monkeypatch.setitem(sys.modules, "coverage", _fake_module(store))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("ENGKIT_COVERAGE_DATA", str(tmp_path / ".coverage"))
    monkeypatch.delenv("ENGKIT_COVERAGE_SOURCE", raising=False)
    instance = coverage_plugin.start()
    assert instance is not None and instance.kwargs["source"] is None


def test_start_config_error(monkeypatch, tmp_path: Path) -> None:
    """显式配置路径非法时静默不采（fail-open）。"""
    broken = tmp_path / "broken.toml"
    broken.write_text("[docs\n", encoding="utf-8")
    monkeypatch.setenv(guard._CONFIG_ENV, str(broken))
    assert coverage_plugin.start() is None


def test_stop_idempotent(monkeypatch) -> None:
    """停止会落盘并复位；重复调用无副作用。"""
    instance = _FakeCoverage()
    monkeypatch.setattr(coverage_plugin, "_coverage", instance)
    coverage_plugin.stop()
    assert (instance.stopped, instance.saved) == (1, 1)
    assert coverage_plugin._coverage is None
    coverage_plugin.stop()
    assert instance.stopped == 1


def test_hooks_delegate(monkeypatch) -> None:
    """三个 pytest 钩子转调守卫（不另写实现）。"""
    calls: list[str] = []
    monkeypatch.setattr(guard, "pytest_addoption", lambda parser: calls.append(f"addoption:{parser}"))
    monkeypatch.setattr(guard, "pytest_configure", lambda config: calls.append(f"configure:{config}"))
    coverage_plugin.pytest_addoption("parser")
    coverage_plugin.pytest_configure("config")
    assert calls == ["addoption:parser", "configure:config"]


def test_sessionfinish_swallows_errors(monkeypatch) -> None:
    """落盘异常不阻断 pytest 收尾。"""

    def boom() -> None:
        raise RuntimeError("落盘失败")

    monkeypatch.setattr(coverage_plugin, "stop", boom)
    coverage_plugin.pytest_sessionfinish(None, 0)
