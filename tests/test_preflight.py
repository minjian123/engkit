"""预检引擎与定向执行器用例。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from engkit import preflight

_PY = f'"{sys.executable}"'
"""解释器命令（带引号，避免 Windows 路径被 shlex 拆分）。"""

PASS_CMD = f"{_PY} -c pass"
"""恒成功的执行器前缀。"""

SLOW_SCRIPT = "import time\nfor i in range(20):\n    print(i, flush=True)\n    time.sleep(0.3)\n"
"""持续输出的慢脚本（用于超时判定）。"""

SLOW_CMD = f'{_PY} -c "import time\nfor i in range(20):\n    print(i, flush=True)\n    time.sleep(0.3)"'
"""持续输出的慢命令（供执行器超时判定）。"""


@pytest.fixture(autouse=True)
def _ci(monkeypatch):
    """预检默认在 CI 环境下运行（跳过未包装拦截）。"""
    monkeypatch.setenv("CI", "1")


def test_jobs_args() -> None:
    """并行档位解析。"""
    assert preflight.jobs_args("auto") == ("-n", "auto")
    assert preflight.jobs_args("4") == ("-n", "4")
    assert preflight.jobs_args("off") == ()
    assert preflight.jobs_args("") == ()
    with pytest.raises(ValueError, match="取值非法"):
        preflight.jobs_args("abc")


def test_describe_jobs() -> None:
    """并行档位描述。"""
    assert "并行 -n auto" in preflight.describe_jobs("auto")
    assert preflight.describe_jobs("off") == "串行（--jobs off）"


def test_expand(cfg) -> None:
    """占位符替换。"""
    cmd = preflight.expand(cfg, ("{python}", "-c", "print('{root}')", "{config}", "plain"))
    assert cmd[0] == sys.executable
    assert cmd[3] == str(cfg.path)
    assert cmd[4] == "plain"


def test_step_applies(cfg) -> None:
    """档位过滤。"""
    steps = {step.label: step for step in cfg.preflight.steps}
    assert preflight.step_applies(steps["静态：echo"], "scoped") is True
    assert preflight.step_applies(steps["快档专用"], "fast") is True
    assert preflight.step_applies(steps["快档专用"], "scoped") is False


def test_execute_success(tmp_path: Path) -> None:
    """命令执行成功并收集输出。"""
    failures: list[str] = []
    captured: list[str] = []
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    ok = preflight.execute("echo", [sys.executable, "-c", "print('hi')"], tmp_path, budget, failures, capture=captured)
    assert ok is True
    assert failures == []
    assert captured == ["hi"]


def test_execute_failure(tmp_path: Path) -> None:
    """命令失败计入失败项。"""
    failures: list[str] = []
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    assert preflight.execute("bad", [sys.executable, "-c", "raise SystemExit(2)"], tmp_path, budget, failures) is False
    assert failures == ["bad"]


def test_execute_optional_missing(tmp_path: Path) -> None:
    """可选步骤的命令缺失只跳过。"""
    failures: list[str] = []
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    assert preflight.execute("opt", ["definitely-missing-binary"], tmp_path, budget, failures, optional=True) is True
    assert failures == []
    assert preflight.execute("req", ["definitely-missing-binary"], tmp_path, budget, failures) is False
    assert failures


def test_execute_timeout(tmp_path: Path, capsys) -> None:
    """单步超时即中止并计入失败。"""
    failures: list[str] = []
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    ok = preflight.execute(
        "slow", [sys.executable, "-c", "import time; time.sleep(3)"], tmp_path, budget, failures, timeout=0.2
    )
    assert ok is False
    assert "单步超时" in failures[0]


def test_execute_budget_exhausted(tmp_path: Path) -> None:
    """总体预算耗尽即中止（不再执行后续步骤）。"""
    failures: list[str] = []
    budget = preflight.Budget(step_seconds=0.0, total_seconds=0.0)
    assert preflight.execute("x", [sys.executable, "-c", "pass"], tmp_path, budget, failures) is False
    assert budget.exhausted is True
    assert preflight.execute("y", [sys.executable, "-c", "pass"], tmp_path, budget, failures) is False


def test_self_test_selected(cfg) -> None:
    """自测按需判定（命中依赖 / 未命中）。"""
    entry = cfg.preflight.self_tests[0]
    assert preflight._self_test_selected(entry, ["scripts/tools/guard.py"]) is True
    assert preflight._self_test_selected(entry, ["scripts/tools/guard.py/x.py"]) is True
    assert preflight._self_test_selected(entry, ["docs/a.md"]) is False


def test_run_self_tests_force_all(cfg) -> None:
    """`--fast` / `--full` 档全跑。"""
    failures: list[str] = []
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    preflight.run_self_tests(cfg, budget, failures, force_all=True, mode="fast")
    assert failures == []


def test_run_self_tests_by_change(cfg, monkeypatch) -> None:
    """定向档按变更按需执行（命中 / 未命中）。"""
    failures: list[str] = []
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    monkeypatch.setattr(preflight, "_changes", lambda config: (["scripts/tools/guard.py"], True))
    preflight.run_self_tests(cfg, budget, failures, force_all=False, mode="scoped")
    assert failures == []
    monkeypatch.setattr(preflight, "_changes", lambda config: (["docs/a.md"], True))
    preflight.run_self_tests(cfg, budget, failures, force_all=False, mode="scoped")
    assert failures == []


def test_run_self_tests_unreliable(cfg, monkeypatch) -> None:
    """变更不可判定时全跑（不静默跳过）。"""
    failures: list[str] = []
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    monkeypatch.setattr(preflight, "_changes", lambda config: ([], False))
    preflight.run_self_tests(cfg, budget, failures, force_all=False, mode="scoped")
    assert failures == []


def test_gate_command(cfg) -> None:
    """门禁命令在配置的解释器里跑 engkit 自身。"""
    argv = preflight.gate_command(cfg, cfg.root / ".coverage", cfg.root / "r.json", ["pkg"], 100)
    assert argv[0] == "python"
    assert "engkit_main" in argv[-1]
    assert "--module" in argv[-1]


def test_run_pytest_once(tmp_path: Path) -> None:
    """单轮执行与超时。"""
    code, lines = preflight.run_pytest_once([sys.executable, "-c", "print('ok')"], tmp_path, None, None)
    assert code == 0
    assert lines == ["ok"]
    with pytest.raises(subprocess.TimeoutExpired):
        preflight.run_pytest_once([sys.executable, "-c", SLOW_SCRIPT], tmp_path, None, 0.2)


def test_metrics_summary() -> None:
    """度量行解析。"""
    lines = ["x", "[test-selection] picked=2/10 degraded=false rounds=1 cov=3 模块", "y"]
    assert preflight.metrics_summary(lines) == "picked=2/10（20.0%）degraded=false rounds=1 cov=3 模块"
    assert preflight.metrics_summary(["none"]) == ""
    assert "—" in preflight.metrics_summary(["[test-selection] picked=0/0 degraded=true rounds=1 cov=0 模块"])


def test_run_selected_invalid_jobs(cfg) -> None:
    """非法并行档位即退出 1。"""
    assert preflight.run_selected(cfg, changed=[], jobs="abc") == 1


def test_run_selected_no_tests(cfg) -> None:
    """无受影响测试时跳过 pytest。"""
    assert preflight.run_selected(cfg, changed=["docs/文档首页.md"]) == 0


def test_run_selected_bad_ref(cfg) -> None:
    """基准不可解析 ⇒ 退化全量并按退化集执行。"""
    code = preflight.run_selected(cfg, changed_from="no-such-ref", pytest_cmd=PASS_CMD, cov=False)
    assert code == 0


def test_run_selected_no_backend(tmp_path: Path) -> None:
    """未配置后端即退出 1。"""
    from engkit.config import load

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "engkit.toml").write_text('[docs]\nroot = "docs"\n', encoding="utf-8")
    (repo / "docs").mkdir()
    assert preflight.run_selected(load(repo / "engkit.toml"), changed=[]) == 1


def test_run_selected_cov_pass(cfg, monkeypatch) -> None:
    """覆盖率门禁通过即返回 0。"""
    monkeypatch.setattr(preflight, "gate_command", lambda *args: [sys.executable, "-c", "raise SystemExit(0)"])
    code = preflight.run_selected(
        cfg,
        changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
        pytest_cmd=PASS_CMD,
    )
    assert code == 0


def test_run_selected_gate_error(cfg, monkeypatch) -> None:
    """门禁环境错误即透传其退出码。"""
    monkeypatch.setattr(preflight, "gate_command", lambda *args: [sys.executable, "-c", "raise SystemExit(2)"])
    assert preflight.run_selected(cfg, changed=["backend/libs/pkg_a/src/pkg_a/util.py"], pytest_cmd=PASS_CMD) == 2


def test_run_selected_gate_fail_without_missing(cfg, monkeypatch) -> None:
    """覆盖率未达标但报告无缺行 ⇒ 退出 1。"""
    monkeypatch.setattr(preflight, "gate_command", lambda *args: [sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(preflight.selection, "expand_from_coverage", lambda path, modules: [])
    assert preflight.run_selected(cfg, changed=["backend/libs/pkg_a/src/pkg_a/util.py"], pytest_cmd=PASS_CMD) == 1


def test_run_selected_expand_then_pass(cfg, monkeypatch) -> None:
    """扩圈增长后重跑并通过。"""
    calls = {"n": 0}

    def gate(*args):
        calls["n"] += 1
        code = 1 if calls["n"] == 1 else 0
        return [sys.executable, "-c", f"raise SystemExit({code})"]

    import dataclasses

    not_degraded = dataclasses.replace(cfg, base_ratio=1.0)
    monkeypatch.setattr(preflight, "gate_command", gate)
    monkeypatch.setattr(preflight.selection, "expand_from_coverage", lambda path, modules: ["pkg_a.util"])
    monkeypatch.setattr(
        preflight.selection, "tests_for_modules", lambda config, modules: ["libs/pkg_a/tests/boundary/test_boundary.py"]
    )
    code = preflight.run_selected(not_degraded, changed=["backend/libs/pkg_a/src/pkg_a/util.py"], pytest_cmd=PASS_CMD)
    assert code == 0
    assert calls["n"] == 2


def test_run_selected_degrade_full(cfg, monkeypatch) -> None:
    """扩圈无新增 ⇒ 退化工程级全量重跑；仍不达标即退出 1。"""
    import dataclasses

    monkeypatch.setattr(preflight, "gate_command", lambda *args: [sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(preflight.selection, "expand_from_coverage", lambda path, modules: ["pkg_a.util"])
    monkeypatch.setattr(preflight.selection, "tests_for_modules", lambda config, modules: [])
    code = preflight.run_selected(
        dataclasses.replace(cfg, base_ratio=1.0),
        changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
        pytest_cmd=PASS_CMD,
    )
    assert code == 1


def test_run_selected_rounds_exhausted(cfg, monkeypatch) -> None:
    """扩圈轮次用尽 ⇒ 退化工程级全量。"""
    import dataclasses

    monkeypatch.setattr(preflight, "gate_command", lambda *args: [sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(preflight.selection, "expand_from_coverage", lambda path, modules: ["pkg_a.util"])
    monkeypatch.setattr(
        preflight.selection, "tests_for_modules", lambda config, modules: ["libs/pkg_a/tests/boundary/test_boundary.py"]
    )
    code = preflight.run_selected(
        dataclasses.replace(cfg, base_ratio=1.0),
        changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
        pytest_cmd=PASS_CMD,
        max_rounds=0,
    )
    assert code == 1


def test_run_selected_pytest_failure(cfg) -> None:
    """测试失败即透传退出码（不进扩圈）。"""
    assert (
        preflight.run_selected(
            cfg,
            changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
            pytest_cmd=f'{_PY} -c "raise SystemExit(5)"',
        )
        == 5
    )


def test_run_selected_timeout(cfg) -> None:
    """单轮超时返回 124。"""
    code = preflight.run_selected(
        cfg,
        changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
        pytest_cmd=SLOW_CMD,
        timeout=0.2,
    )
    assert code == preflight.TIMEOUT_CODE


def test_run_selected_full_retried(cfg, monkeypatch) -> None:
    """已退化全量后扩圈仍不收敛 ⇒ 直接退出 1（不再重复退化）。"""
    monkeypatch.setattr(preflight, "gate_command", lambda *args: [sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(preflight.selection, "expand_from_coverage", lambda path, modules: ["pkg_a.util"])
    monkeypatch.setattr(preflight.selection, "tests_for_modules", lambda config, modules: [])
    code = preflight.run_selected(
        cfg,
        changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
        pytest_cmd=PASS_CMD,
        force_full=True,
    )
    assert code == 1


def test_run_scoped_step(cfg, monkeypatch) -> None:
    """定向段打印度量汇总并把失败计入失败项。"""
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    failures: list[str] = []

    def fake_run_selected(*args, **kwargs):
        print("[test-selection] picked=1/4 degraded=false rounds=1 cov=1 模块")
        return 0

    monkeypatch.setattr(preflight, "run_selected", fake_run_selected)
    preflight._run_scoped_step(
        cfg, budget, failures, jobs="auto", no_cov=False, changes=["backend/libs/pkg_a/src/pkg_a/util.py"]
    )
    assert failures == []


def test_run_scoped_step_failure(cfg, monkeypatch) -> None:
    """定向段失败登记失败项。"""
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    failures: list[str] = []
    monkeypatch.setattr(preflight, "run_selected", lambda *args, **kwargs: 3)
    preflight._run_scoped_step(
        cfg, budget, failures, jobs="auto", no_cov=True, changes=["backend/libs/pkg_a/src/pkg_a/core.py"]
    )
    assert failures


def test_run_scoped_step_no_tests(cfg) -> None:
    """无后端变更时跳过定向段。"""
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    failures: list[str] = []
    preflight._run_scoped_step(cfg, budget, failures, jobs="auto", no_cov=False, changes=["docs/a.md"])
    assert failures == []


def test_run_scoped_step_budget_exhausted(cfg) -> None:
    """预算耗尽即登记失败。"""
    budget = preflight.Budget(step_seconds=0.0, total_seconds=0.0)
    failures: list[str] = []
    preflight._run_scoped_step(
        cfg, budget, failures, jobs="auto", no_cov=False, changes=["backend/libs/pkg_a/src/pkg_a/util.py"]
    )
    assert budget.exhausted is True
    assert failures


def test_run_scoped_step_backend_error(tmp_path: Path) -> None:
    """未配置后端时定向段登记失败而不抛错。"""
    from engkit.config import load

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "engkit.toml").write_text('[docs]\nroot = "docs"\n', encoding="utf-8")
    (repo / "docs").mkdir()
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    failures: list[str] = []
    preflight._run_scoped_step(load(repo / "engkit.toml"), budget, failures, jobs="auto", no_cov=False, changes=[])
    assert failures


def test_run_full_and_project_tests(cfg, monkeypatch) -> None:
    """全量档：聚合全量 + 覆盖率门禁 + 逐工程范围。"""
    calls: list[str] = []

    def fake_execute(label, argv, cwd, budget, failures, **kwargs):
        calls.append(label)
        return True

    monkeypatch.setattr(preflight, "execute", fake_execute)
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    failures: list[str] = []
    preflight.run_full_tests(cfg, budget, failures, jobs="off", no_cov=False)
    preflight.run_full_tests(cfg, budget, failures, jobs="off", no_cov=True)
    preflight.run_project_tests(cfg, budget, failures, jobs="off")
    assert any("聚合全量" in item for item in calls)
    assert any("工程级范围" in item for item in calls)
    assert failures == []


def test_run_project_tests_without_backend(tmp_path: Path, monkeypatch) -> None:
    """未配置后端时逐工程范围为空操作。"""
    from engkit.config import load

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "engkit.toml").write_text('[docs]\nroot = "docs"\n', encoding="utf-8")
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    failures: list[str] = []
    preflight.run_project_tests(load(repo / "engkit.toml"), budget, failures, jobs="off")
    assert failures == []


def test_self_test(cfg) -> None:
    """引擎自检通过。"""
    assert preflight.self_test(cfg) == 0


def test_self_test_failure(build_repo) -> None:
    """工程未登记即自检失败。"""
    from engkit.config import load

    root = build_repo(config='[docs]\nroot = "docs"\n[backend]\nroot = "backend"\n[preflight]\nsteps = []\n')
    assert preflight.self_test(load(root / "engkit.toml")) == 1


def test_run_preflight_blocked(monkeypatch, cfg) -> None:
    """未包装即拦截（命令零执行）。"""
    monkeypatch.delenv("CI")
    assert preflight.run_preflight(cfg, mode="fast", jobs="off", no_cov=False, no_scope=False) == 1


def test_run_preflight_invalid_jobs(cfg) -> None:
    """非法并行档位返回 1。"""
    assert preflight.run_preflight(cfg, mode="fast", jobs="abc", no_cov=False, no_scope=False) == 1


def test_run_preflight_fast(cfg) -> None:
    """快速档执行静态步骤与全跑自测。"""
    assert preflight.run_preflight(cfg, mode="fast", jobs="off", no_cov=True, no_scope=True) == 0


def test_run_preflight_budget_zero(cfg) -> None:
    """预算为零即中止并判失败。"""
    code = preflight.run_preflight(
        cfg, mode="fast", jobs="off", no_cov=True, no_scope=True, step_seconds=0.0, budget_seconds=0.0
    )
    assert code == 1


def test_run_preflight_full_steps(cfg, monkeypatch) -> None:
    """全量档执行 full / scoped_tests / project_tests 三类步骤。"""
    from engkit.config import Step

    steps = (
        Step(label="定向", type="scoped_tests", preset="scoped"),
        Step(label="全量", type="full_tests", preset="full"),
        Step(label="逐工程", type="project_tests", preset="full"),
    )
    import dataclasses

    patched = dataclasses.replace(cfg, preflight=dataclasses.replace(cfg.preflight, steps=steps, self_tests=()))
    monkeypatch.setattr(preflight, "_run_scoped_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight, "run_full_tests", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight, "run_project_tests", lambda *args, **kwargs: None)
    assert preflight.run_preflight(patched, mode="scoped", jobs="off", no_cov=False, no_scope=False) == 0
    assert preflight.run_preflight(patched, mode="full", jobs="off", no_cov=False, no_scope=True) == 0


def test_tee(capsys) -> None:
    """输出同时进原流与缓冲。"""
    sink: list[str] = []
    import sys as system_sys

    tee = preflight.Tee(system_sys.stdout, sink)
    tee.write("a\nb")
    tee.flush()
    assert sink == ["a"]
    assert "a" in capsys.readouterr().out
