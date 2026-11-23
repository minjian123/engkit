"""定向档失败汇总用例（一批失败不早退、跑完再汇总，Kiwi 2545）。"""

from __future__ import annotations

import dataclasses

import pytest

from engkit import preflight

pytestmark = pytest.mark.kiwi_id(2545)

_PASS_LINES = ["2 passed in 0.10s"]
"""通过批输出（含结论行）。"""


@pytest.fixture
def tiny_batches(cfg, monkeypatch):
    """把定向档压成「每批一个文件」，便于构造多批形态。

    Args:
        cfg: 迷你仓库配置。
        monkeypatch: pytest 补丁工具。

    Returns:
        preflight.Config: 预算极小的配置（配合身份折叠）。
    """
    monkeypatch.setattr(preflight, "collapse_to_dirs", lambda cwd, paths, *, threshold=None: list(paths))
    monkeypatch.setattr(preflight, "parallel_args", lambda config, jobs, cwd=None: ())
    return dataclasses.replace(cfg, pytest=dataclasses.replace(cfg.pytest, argv_budget=1))


def test_run_selected_continues_after_failed_batch(cfg, tiny_batches, monkeypatch, capsys) -> None:
    """一批失败不早退：后续批照跑，末尾给全「失败批 / 通过批 / 未跑批」，返回 1。"""
    seen: list[int] = []

    def fake_run(argv, cwd, env, timeout):
        seen.append(len(seen))
        index = len(seen)
        if index == 1:
            return 1, ["FAILED a::x - assert", "1 failed, 1 passed in 0.10s"]
        return 0, list(_PASS_LINES)

    monkeypatch.setattr(preflight, "run_pytest_once", fake_run)
    code = preflight.run_selected(
        tiny_batches,
        changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
        pytest_cmd="pytest",
        cov=False,
        budget_probe=lambda: 100.0,
    )
    assert code == 1
    assert len(seen) >= 2
    text = capsys.readouterr().out
    assert "[test-selection] 失败批：第 1 批（第 1 批退出码 1）" in text
    assert "[test-selection] 通过批：" in text
    assert "[test-selection] 未跑批：无" in text
    assert "不进入覆盖率判定与扩圈" in text


def test_run_selected_stops_when_budget_exhausted(tiny_batches, monkeypatch, capsys) -> None:
    """失败后剩余预算 ≤0 ⇒ 停后续批，未跑批逐条列出（不伪装成已跑）。"""
    calls: list[int] = []

    def fake_run(argv, cwd, env, timeout):
        calls.append(1)
        return 1, ["1 failed in 0.10s"]

    monkeypatch.setattr(preflight, "run_pytest_once", fake_run)
    code = preflight.run_selected(
        tiny_batches,
        changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
        pytest_cmd="pytest",
        cov=False,
        budget_probe=lambda: 0.0,
    )
    assert code == 1
    assert len(calls) == 1
    text = capsys.readouterr().out
    assert "剩余预算已耗尽" in text
    assert "未跑批：第 " in text


def test_run_selected_all_batches_pass_keeps_behavior(cfg, monkeypatch) -> None:
    """全部批通过 ⇒ 行为不变（返回 0，无条件进入原有判定路径）。"""
    monkeypatch.setattr(preflight, "run_pytest_once", lambda *args: (0, list(_PASS_LINES)))
    code = preflight.run_selected(cfg, changed=["backend/libs/pkg_a/src/pkg_a/util.py"], pytest_cmd="pytest", cov=False)
    assert code == 0


def test_print_batch_summary_reports_counts_services_and_locks(capsys) -> None:
    """批级摘要：目标数 / 服务构成 / 通过失败错误 / `database is locked` 次数。"""
    batch = [
        "services/a/tests/test_1.py",
        "services/a/tests/test_2.py",
        "services/b/tests/test_3.py",
        "libs/c/tests/test_4.py",
        "tests/test_5.py",
    ]
    lines = [
        "database is locked",
        "sqlite3.OperationalError: database is locked",
        "2 failed, 3 passed, 1 error in 12.30s",
    ]
    preflight.print_batch_summary(2, 3, batch, lines, 12.34)
    text = capsys.readouterr().out
    assert "第 2/3 批完成：目标 5" in text
    assert "服务 a/b/libs-c" in text
    assert "12.3s" in text
    assert "3 通过 / 2 失败 / 1 错误" in text
    assert "database is locked 2 次" in text


def test_batch_counts_and_services_edges() -> None:
    """无结论行 ⇒ 计数全 0；空批服务构成显示占位符。"""
    assert preflight.batch_counts(["no summary here"]) == (0, 0, 0)
    assert preflight.batch_counts([]) == (0, 0, 0)
    assert preflight.batch_services([]) == "—"


def test_batch_labels() -> None:
    """批序号列举：空列表为「无」，多批按中文顿号连接。"""
    assert preflight._batch_labels([]) == "无"
    assert preflight._batch_labels([1, 3]) == "第 1、3 批"


def test_run_scoped_step_passes_budget_probe(cfg, monkeypatch) -> None:
    """定向段把「剩余预算探针」交给执行器：余额扣除本步已耗。"""
    captured: dict[str, object] = {}

    def fake_run_selected(*args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(preflight, "run_selected", fake_run_selected)
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    failures: list[str] = []
    preflight._run_scoped_step(
        cfg, budget, failures, jobs="auto", no_cov=True, changes=["backend/libs/pkg_a/src/pkg_a/util.py"]
    )
    probe = captured.get("budget_probe")
    assert callable(probe)
    assert 0 < probe() <= 30
