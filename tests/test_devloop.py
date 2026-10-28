"""开发环用例：改动文件 ruff、后端直接依赖测试、前端受影响包与编排。"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from conftest import init_git

from engkit import devloop
from engkit.config import Config, load


def _with_ruff(cfg: Config, cmd: str = "ruff", config: str = "") -> Config:
    """给配置补上 ruff 执行器。

    Args:
        cfg: 配置。
        cmd: ruff 执行器前缀。
        config: ruff 配置路径。

    Returns:
        Config: 替换后的配置。
    """
    return dataclasses.replace(cfg, ruff_cmd=cmd, ruff_config=config)


def test_chunks() -> None:
    """按固定大小切分。"""
    assert devloop._chunks([], 2) == []
    assert devloop._chunks(["a", "b", "c"], 2) == [["a", "b"], ["c"]]


def test_exec_missing_command(tmp_path: Path) -> None:
    """命令不存在时返回 1。"""
    assert devloop._exec(["definitely-missing-binary"], tmp_path) == 1


def test_lint_changed_no_ruff(repo: Path, cfg) -> None:
    """未配置 ruff 执行器时跳过。"""
    (repo / "x.py").write_text("X = 1\n", encoding="utf-8")
    assert devloop._lint_changed(cfg, ["x.py"]) == 0


def test_lint_changed_no_python(repo: Path, cfg) -> None:
    """改动不含 Python 文件时跳过。"""
    assert devloop._lint_changed(_with_ruff(cfg), ["docs/文档首页.md"]) == 0


def test_lint_changed_runs_check_and_format(repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """check 与 format --check 各跑一次，带 `--config`。"""
    calls: list[list[str]] = []

    def fake(argv: list[str], cwd: Path) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(devloop, "_exec", fake)
    (repo / "x.py").write_text("X = 1\n", encoding="utf-8")
    assert devloop._lint_changed(_with_ruff(cfg, "uv run ruff", "backend/pyproject.toml"), ["x.py"]) == 0
    assert calls == [
        ["uv", "run", "ruff", "check", "--config", "backend/pyproject.toml", "x.py"],
        ["uv", "run", "ruff", "format", "--check", "--config", "backend/pyproject.toml", "x.py"],
    ]


def test_lint_changed_check_fails(repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """check 失败即返回，不再跑 format。"""
    monkeypatch.setattr(devloop, "_exec", lambda argv, cwd: 3)
    (repo / "x.py").write_text("X = 1\n", encoding="utf-8")
    assert devloop._lint_changed(_with_ruff(cfg), ["x.py"]) == 3


def test_lint_changed_format_fails(repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """format 失败即返回其退出码。"""
    codes = iter([0, 4])

    monkeypatch.setattr(devloop, "_exec", lambda argv, cwd: next(codes))
    (repo / "x.py").write_text("X = 1\n", encoding="utf-8")
    assert devloop._lint_changed(_with_ruff(cfg), ["x.py"]) == 4


def test_frontend_changed_unconfigured(cfg) -> None:
    """未配置前端时跳过。"""
    patched = dataclasses.replace(cfg, frontend=None)
    assert devloop._frontend_changed(patched, []) == 0


def test_frontend_changed_no_packages(cfg) -> None:
    """无受影响前端包时跳过。"""
    assert devloop._frontend_changed(cfg, ["docs/文档首页.md"]) == 0


def test_frontend_changed_runs_packages(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """受影响包逐个执行（`{package}` 替换，含依赖反向闭包）。"""
    calls: list[list[str]] = []

    def fake(argv: list[str], cwd: Path) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(devloop, "_exec", fake)
    assert devloop._frontend_changed(cfg, ["frontend/packages/core/src/x.ts"]) == 0
    assert calls == [
        ["pnpm", "--filter", "@mini/core", "test"],
        ["pnpm", "--filter", "@mini/desktop", "test"],
    ]


def test_frontend_changed_failure_stops(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """前端包失败即返回其退出码。"""
    monkeypatch.setattr(devloop, "_exec", lambda argv, cwd: 5)
    assert devloop._frontend_changed(cfg, ["frontend/packages/core/src/x.ts"]) == 5


def test_run_changed_read_error(cfg) -> None:
    """基准不可解析时返回 1。"""
    assert devloop.run_changed(cfg, changed_from="no-such-ref") == 1


def test_run_changed_no_changes(repo: Path) -> None:
    """无变更即结束（独立 git 仓 + LF，避免行尾被误判为改动）。"""
    for path in repo.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    init_git(repo)
    assert devloop.run_changed(load(repo / "engkit.toml"), changed_from="HEAD") == 0


def test_run_changed_lint_failure(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """静态失败：不跑测试，直接返回。"""
    calls: list[str] = []
    monkeypatch.setattr(devloop, "_lint_changed", lambda cfg, changed: 2)
    monkeypatch.setattr(devloop.preflight, "run_selected", lambda *a, **k: calls.append("pytest") or 0)
    assert devloop.run_changed(cfg, changed_files=("a.py",)) == 2
    assert calls == []


def test_run_changed_backend_failure(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """后端测试失败：不跑前端，返回其退出码。"""
    monkeypatch.setattr(devloop, "_lint_changed", lambda cfg, changed: 0)
    monkeypatch.setattr(devloop.preflight, "run_selected", lambda *a, **k: 7)
    monkeypatch.setattr(devloop, "_frontend_changed", lambda cfg, changed: 0)
    assert devloop.run_changed(cfg, changed_files=("a.py",)) == 7


def test_run_changed_frontend_failure(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """前端失败返回其退出码。"""
    monkeypatch.setattr(devloop, "_lint_changed", lambda cfg, changed: 0)
    monkeypatch.setattr(devloop.preflight, "run_selected", lambda *a, **k: 0)
    monkeypatch.setattr(devloop, "_frontend_changed", lambda cfg, changed: 9)
    assert devloop.run_changed(cfg, changed_files=("a.py",)) == 9


def test_run_changed_all_pass(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """全环节通过，且后端按直接依赖深度执行。"""
    seen: list[dict[str, object]] = []

    def fake(cfg, **kwargs):
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(devloop, "_lint_changed", lambda cfg, changed: 0)
    monkeypatch.setattr(devloop.preflight, "run_selected", fake)
    monkeypatch.setattr(devloop, "_frontend_changed", lambda cfg, changed: 0)
    assert devloop.run_changed(cfg, changed_files=("a.py",)) == 0
    assert seen[0]["depth"] == "direct"
    assert seen[0]["cov"] is False


def test_run_changed_skips_lint_and_frontend(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--no-lint` / `--no-frontend` 生效。"""
    touched: list[str] = []
    monkeypatch.setattr(devloop, "_lint_changed", lambda cfg, changed: touched.append("lint") or 0)
    monkeypatch.setattr(devloop.preflight, "run_selected", lambda *a, **k: 0)
    monkeypatch.setattr(devloop, "_frontend_changed", lambda cfg, changed: touched.append("frontend") or 0)
    assert devloop.run_changed(cfg, changed_files=("a.py",), lint=False, frontend=False) == 0
    assert touched == []
