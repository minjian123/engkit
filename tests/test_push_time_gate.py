"""推送时长闸门与测试分层（工程化工具包说明 §4.7）的正反例。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from conftest import git

from engkit import preflight, push


def _remote(git_repo: Path, tmp_path: Path) -> Path:
    """造一个 bare 远端并把当前提交推上去（之后新增的提交才算「待推」）。

    Args:
        git_repo: 已提交的迷你仓库。
        tmp_path: pytest 临时目录。

    Returns:
        Path: bare 远端路径。
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(git_repo, "remote", "add", "origin", str(remote))
    git(git_repo, "push", "-q", "-u", "origin", "HEAD")
    return remote


def _commit(git_repo: Path, rel: str, content: str = "x\n") -> None:
    """写文件并提交。

    Args:
        git_repo: 仓库根。
        rel: 相对路径。
        content: 文件内容。
    """
    path = git_repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", f"add {rel}")


def _head(repo: Path) -> str:
    """取仓库 HEAD。

    Args:
        repo: 仓库根或 bare 远端。

    Returns:
        str: 提交号。
    """
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_cap_push_budget() -> None:
    """600s 是常量；CLI 超大预算被封顶，CI 手动预检不受限。"""
    assert preflight.PUSH_MAX_TOTAL_SECONDS == 600.0
    assert preflight.cap_push_budget(2400.0, 9000.0, from_push=True) == (600.0, 600.0)
    assert preflight.cap_push_budget(300.0, 420.0, from_push=True) == (300.0, 420.0)
    assert preflight.cap_push_budget(900.0, 1800.0, from_push=False) == (900.0, 1800.0)


def test_serial_reason_values() -> None:
    """串行取值一律命中，并行取值放行。"""
    for value in ("off", "none", "0", "", "  OFF  "):
        assert push.serial_reason(value) != ""
    assert push.serial_reason("auto") == ""
    assert push.serial_reason("4") == ""


def test_serial_reason_addopts(monkeypatch) -> None:
    """`PYTEST_ADDOPTS=-n 0` 同样按人为串行化拒绝。"""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 0")
    assert "-n 0" in push.serial_reason("auto")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 4")
    assert push.serial_reason("auto") == ""


def test_run_push_rejects_serial_jobs(git_repo: Path, cfg, capsys) -> None:
    """`--jobs off` 立即拒绝、零步骤执行。"""
    assert push.run_push(cfg, jobs="off") == 1
    output = capsys.readouterr().out
    assert "拒绝执行" in output
    assert "串行" in output
    assert "时长闸门" not in output


def test_run_push_rejects_addopts_serial(git_repo: Path, cfg, capsys, monkeypatch) -> None:
    """环境变量里的 `-n 0` 也拦在预检之前。"""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 0")
    assert push.run_push(cfg, jobs="auto") == 1
    assert "PYTEST_ADDOPTS" in capsys.readouterr().out


def test_run_push_denies_git_push_when_gate_exceeded(git_repo: Path, cfg, tmp_path: Path, capsys, monkeypatch) -> None:
    """即使预检全绿，只要全链路超过硬上限就绝不执行 git push。"""
    remote = _remote(git_repo, tmp_path)
    _commit(git_repo, "docs/项目/记录.md")
    before = _head(remote)
    monkeypatch.setattr(preflight, "PUSH_MAX_TOTAL_SECONDS", 0.0)
    monkeypatch.setattr(preflight, "run_preflight", lambda *args, **kwargs: 0)
    assert push.run_push(cfg, jobs="auto") == 1
    output = capsys.readouterr().out
    assert "拒绝推送" in output
    assert "本次不执行 git push" in output
    assert "推送完成" not in output
    assert _head(remote) == before


def test_run_preflight_caps_cli_budget(cfg, capsys, monkeypatch) -> None:
    """CLI 传 9000s 也不越过 600s 硬上限。"""
    from engkit.preflight_gate import Decision

    # 迷你仓库无 .git，闸门会向上探测到工具包仓自身的工作树；本用例只验封顶，故放行闸门。
    monkeypatch.setattr(preflight.preflight_gate, "evaluate", lambda _cfg: Decision(blocked=False))
    code = preflight.run_preflight(
        cfg,
        mode="fast",
        jobs="auto",
        no_cov=True,
        no_scope=True,
        step_seconds=2400.0,
        budget_seconds=9000.0,
        from_push=True,
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "时长闸门" in output
    assert "总体 600s" in output
    assert "耗时汇总" in output


def test_print_timing_summary(capsys) -> None:
    """耗时汇总含测试段 / 逐轮 / 静态合计 / 最慢 5 步 / 交 CI / 审计耗时。"""
    budget = preflight.Budget(step_seconds=300.0, total_seconds=600.0)
    budget.record("静态：ruff", 3.0)
    budget.record("后端测试：定向（选测 + 覆盖率门禁 + 扩圈）", 120.0, "tests")
    budget.test_rounds.extend([80.0, 40.0])
    budget.ci_offload = ("svc：本地选中 1/4，其余 3 个测试文件交 CI 全量回归",)
    preflight.print_timing_summary(budget, time.monotonic() - 123.0, from_push=True)
    output = capsys.readouterr().out
    for fragment in (
        "耗时汇总",
        "总耗时",
        "测试段：120.0s",
        "第 1 轮 80.0s",
        "静态与护栏步骤：合计 3.0s",
        "最慢 5 步",
        "交 CI 全量回归：1 项",
        "duration_seconds=",
    ):
        assert fragment in output
    assert "拒绝：" not in output


def test_print_timing_summary_diagnosis_when_exhausted(capsys) -> None:
    """预算耗尽即给硬拒诊断，不放宽上限。"""
    budget = preflight.Budget(step_seconds=300.0, total_seconds=600.0)
    budget.exhausted = True
    preflight.print_timing_summary(budget, time.monotonic() - 601.0, from_push=True)
    output = capsys.readouterr().out
    assert "拒绝：单次 push 全链路超过 600s 硬上限" in output
    assert "不自动重试" in output


def test_scoped_step_offloads_to_ci(cfg, monkeypatch) -> None:
    """推送环定向段关退化全量、开交 CI 登记，并把耗时记入预算。"""
    captured: dict[str, object] = {}

    def fake_run_selected(*args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(preflight, "run_selected", fake_run_selected)
    budget = preflight.Budget(step_seconds=300.0, total_seconds=600.0)
    failures: list[str] = []
    preflight._run_scoped_step(
        cfg,
        budget,
        failures,
        jobs="auto",
        no_cov=False,
        changes=["backend/libs/pkg_a/src/pkg_a/util.py"],
    )
    assert captured["allow_full"] is False
    assert captured["offload_to_ci"] is True
    assert not failures
    assert any(kind == "tests" for _label, _seconds, kind in budget.timings)


def test_run_selected_offload_authorizes_internal_command(cfg, monkeypatch) -> None:
    """分层归位下定向集成用例仍需内部命令授权，避免被全量守卫误拦。"""
    captured: dict[str, object] = {}

    def fake_pytest(argv, workdir, env, limit):
        captured["env"] = env
        return 0, []

    monkeypatch.setattr(preflight, "run_pytest_once", fake_pytest)
    code = preflight.run_selected(
        cfg,
        changed=["backend/libs/pkg_a/tests/core/test_core.py"],
        cov=False,
        offload_to_ci=True,
        pytest_cmd=f'"{sys.executable}" -c pass',
    )
    assert code == 0
    env = captured["env"]
    assert env is not None
    assert env[preflight.guard.FULL_AUTH_ENV] == "1"


def test_run_push_uses_full_step_budget(git_repo: Path, cfg, tmp_path: Path, monkeypatch) -> None:
    """未指定预算时单步与总体都取 600s，不被仓侧 scoped 档提前截断。"""
    _remote(git_repo, tmp_path)
    _commit(git_repo, "docs/项目/记录.md")
    captured: dict[str, object] = {}

    def fake_preflight(*args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(preflight, "run_preflight", fake_preflight)
    assert push.run_push(cfg, jobs="auto") == 0
    assert captured["step_seconds"] == 600.0
    assert captured["budget_seconds"] == 600.0
    assert captured["wall_started"] is not None
