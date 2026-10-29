"""预检闸门与调用审计用例。"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from engkit import gitutil, guard, preflight, preflight_gate
from engkit.config import PreflightGateConfig


def _no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空全部 CI 标识（闸门只在非 CI 环境生效）。

    Args:
        monkeypatch: pytest 夹具。
    """
    for name in guard.CI_ENVS:
        monkeypatch.delenv(name, raising=False)


def _gate(cfg, *, dirty_tree: bool = True, audit_file: str = ".cache/preflight/audit.jsonl"):
    """按给定闸门参数复制配置。

    Args:
        cfg: 原配置。
        dirty_tree: 是否启用脏树闸门。
        audit_file: 审计落点。

    Returns:
        复制后的配置。
    """
    return dataclasses.replace(
        cfg,
        preflight=dataclasses.replace(
            cfg.preflight,
            gate=PreflightGateConfig(dirty_tree=dirty_tree, audit_file=audit_file),
        ),
    )


def test_resolve_mode() -> None:
    """无参即快速档；`--full` 优先（定向档由推送入口按变更自动选择）。"""
    assert preflight.resolve_mode(full=False) == "fast"
    assert preflight.resolve_mode(full=True) == "full"


def test_worktree_dirty(git_repo: Path) -> None:
    """已跟踪改动与未跟踪文件都算脏。"""
    assert gitutil.worktree_dirty(git_repo) == ([], True)
    (git_repo / "new.txt").write_text("x", encoding="utf-8")
    (git_repo / "docs" / "文档首页.md").write_text("# 改过\n", encoding="utf-8")
    entries, reliable = gitutil.worktree_dirty(git_repo)
    assert reliable is True
    assert sorted(entries) == ["docs/文档首页.md", "new.txt"]


def test_worktree_dirty_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """git 不可用：不可判定。"""
    monkeypatch.setattr(gitutil, "_run", lambda root, args: None)
    assert gitutil.worktree_dirty(Path(".")) == ([], False)


def test_worktree_dirty_git_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """git 返回非零：不可判定。"""
    fake = subprocess.CompletedProcess([], returncode=128, stdout="", stderr="fatal")
    monkeypatch.setattr(gitutil, "_run", lambda root, args: fake)
    assert gitutil.worktree_dirty(Path(".")) == ([], False)


def test_worktree_dirty_skips_blank_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """空条目与不足状态位的碎片一律丢弃。"""
    fake = subprocess.CompletedProcess([], returncode=0, stdout="\0ab\0 M x.py\0", stderr="")
    monkeypatch.setattr(gitutil, "_run", lambda root, args: fake)
    assert gitutil.worktree_dirty(Path(".")) == (["x.py"], True)


def test_gate_disabled(git_repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """仓配置关闭闸门：脏树也放行。"""
    _no_ci(monkeypatch)
    (git_repo / "new.txt").write_text("x", encoding="utf-8")
    decision = preflight_gate.evaluate(_gate(cfg, dirty_tree=False))
    assert decision.blocked is False
    assert decision.entries == ()


def test_gate_skips_ci(git_repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI 环境不设闸门（流水线里没有「开发中」）。"""
    monkeypatch.setenv("CI", "1")
    (git_repo / "new.txt").write_text("x", encoding="utf-8")
    assert preflight_gate.evaluate(cfg).blocked is False


def test_gate_clean(git_repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """干净工作树放行。"""
    _no_ci(monkeypatch)
    decision = preflight_gate.evaluate(cfg)
    assert decision.blocked is False
    assert decision.entries == ()
    assert decision.reliable is True


def test_gate_rejects_dirty(git_repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """脏工作树拒绝，提示指向开发环与推送入口。"""
    _no_ci(monkeypatch)
    (git_repo / "new.txt").write_text("x", encoding="utf-8")
    decision = preflight_gate.evaluate(cfg)
    assert decision.blocked is True
    assert decision.entries == ("new.txt",)
    assert decision.reason == preflight_gate.REASON_DIRTY
    text = preflight_gate.guidance(decision)
    assert "推送的内部步骤" in text
    assert "run-changed" in text
    assert "engkit push" in text


def test_gate_unreliable(monkeypatch: pytest.MonkeyPatch, cfg) -> None:
    """git 不可判定时放行（宁放过不误拦）。"""
    _no_ci(monkeypatch)
    monkeypatch.setattr(gitutil, "_run", lambda root, args: None)
    decision = preflight_gate.evaluate(cfg)
    assert decision.blocked is False
    assert decision.reliable is False


def test_ahead_git_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """rev-list / diff 失败：不可判定。"""
    monkeypatch.setattr(gitutil, "ref_exists", lambda root, ref: True)
    fake = subprocess.CompletedProcess([], returncode=128, stdout="", stderr="fatal")
    monkeypatch.setattr(gitutil, "_run", lambda root, args: fake)
    assert gitutil.ahead(Path("."), "origin", "main") == ([], [], False)


def test_current_branch_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """git 不可用：分支名为空串。"""
    monkeypatch.setattr(gitutil, "_run", lambda root, args: None)
    assert gitutil.current_branch(Path(".")) == ""


def test_guidance_truncates() -> None:
    """脏条目过多时从略。"""
    decision = preflight_gate.Decision(blocked=True, entries=tuple(f"f{index}.py" for index in range(7)))
    text = preflight_gate.guidance(decision)
    assert "其余 2 处从略" in text


def test_audit_path(git_repo: Path, cfg) -> None:
    """审计落点：相对仓根 / 绝对路径 / 空串不记录。"""
    assert preflight_gate.audit_path(cfg) == git_repo / ".cache/preflight/audit.jsonl"
    absolute = _gate(cfg, audit_file=str(git_repo / "abs.jsonl"))
    assert preflight_gate.audit_path(absolute) == git_repo / "abs.jsonl"
    assert preflight_gate.audit_path(_gate(cfg, audit_file="")) is None


def test_audit_write_events(git_repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """两类事件各写一行：被拒 / 真跑。"""
    _no_ci(monkeypatch)
    clean = preflight_gate.Decision(blocked=False)
    refused = preflight_gate.Decision(blocked=True, entries=("a.py", "b.py"), reason=preflight_gate.REASON_DIRTY)
    preflight_gate.write(cfg, event=preflight_gate.EVENT_REJECTED, mode="fast", decision=refused)
    preflight_gate.write(
        cfg,
        event=preflight_gate.EVENT_RUN,
        mode="fast",
        decision=clean,
        duration_seconds=1.234,
        exit_code=0,
    )
    lines = (git_repo / ".cache/preflight/audit.jsonl").read_text(encoding="utf-8").splitlines()
    rejected, run = (json.loads(line) for line in lines)
    assert rejected["event"] == "rejected"
    assert rejected["reason"] == preflight_gate.REASON_DIRTY
    assert rejected["fingerprint"] == ""
    assert rejected["dirty"] == 2
    assert run["exit_code"] == 0
    assert run["duration_seconds"] == 1.234
    assert run["fingerprint"] != ""


def test_audit_off(cfg) -> None:
    """落点为空串即不记录（不抛异常）。"""
    preflight_gate.write(_gate(cfg, audit_file=""), event=preflight_gate.EVENT_RUN, mode="fast")


def test_audit_fail_open(git_repo: Path, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """写盘失败 fail-open（落点父路径是文件）。"""
    blocker = git_repo / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(preflight_gate, "audit_path", lambda config: blocker / "audit.jsonl")
    preflight_gate.write(cfg, event=preflight_gate.EVENT_RUN, mode="fast")


def test_preflight_rejects_dirty(git_repo: Path, cfg, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """脏树上预检零步骤执行并记审计（经推送入口调用）。"""
    _no_ci(monkeypatch)
    monkeypatch.setenv("ENGKIT_RUN_ONCE", "1")
    (git_repo / "dirty.txt").write_text("x", encoding="utf-8")
    code = preflight.run_preflight(cfg, mode="fast", jobs="off", no_cov=True, no_scope=True, from_push=True)
    assert code == 1
    captured = capsys.readouterr()
    assert "预检闸门" in captured.err
    assert "静态：echo" not in captured.out
    lines = (git_repo / ".cache/preflight/audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["event"] == "rejected"


def test_preflight_runs_on_clean(git_repo: Path, cfg, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """干净树上经推送入口调用即真跑，并记一条 run 审计。"""
    _no_ci(monkeypatch)
    monkeypatch.setenv("ENGKIT_RUN_ONCE", "1")
    code = preflight.run_preflight(cfg, mode="fast", jobs="off", no_cov=True, no_scope=True, from_push=True)
    assert code == 0
    assert "静态：echo" in capsys.readouterr().out
    lines = (git_repo / ".cache/preflight/audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["event"] == "run"
