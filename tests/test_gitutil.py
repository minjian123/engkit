"""git 基础能力用例。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from engkit import gitutil


def test_norm() -> None:
    """路径归一：反斜杠与空白。"""
    assert gitutil.norm(" a\\b.py \n") == "a/b.py"


def test_ref_exists(git_repo: Path) -> None:
    """基准引用可解析判定。"""
    assert gitutil.ref_exists(git_repo, "HEAD") is True
    assert gitutil.ref_exists(git_repo, "no-such-ref") is False


def test_changes_with_since(git_repo: Path) -> None:
    """变更清单含已跟踪改动与未跟踪文件（中文路径不被转义）。"""
    (git_repo / "docs" / "文档首页.md").write_text("# 改过\n", encoding="utf-8")
    (git_repo / "new.txt").write_text("x", encoding="utf-8")
    changes, reliable = gitutil.changes(git_repo, "HEAD")
    assert reliable is True
    assert "docs/文档首页.md" in changes
    assert "new.txt" in changes


def test_changes_without_git(monkeypatch) -> None:
    """git 不可用：不可判定且无变更。"""
    monkeypatch.setattr(gitutil, "_run", lambda root, args: None)
    assert gitutil.changes(Path(".")) == ([], False)


def test_changes_git_error(monkeypatch) -> None:
    """git 返回非零：不可判定且无变更。"""
    fake = subprocess.CompletedProcess([], returncode=128, stdout="", stderr="fatal")
    monkeypatch.setattr(gitutil, "_run", lambda root, args: fake)
    assert gitutil.changes(Path(".")) == ([], False)


def test_changes_only_untracked(git_repo: Path) -> None:
    """不给基准时只看未跟踪文件。"""
    (git_repo / "new.txt").write_text("x", encoding="utf-8")
    changes, reliable = gitutil.changes(git_repo)
    assert reliable is True
    assert changes == ["new.txt"]


def test_fingerprint_without_git(monkeypatch) -> None:
    """git 不可用：指纹固定为 `nogit`。"""
    monkeypatch.setattr(gitutil, "_run", lambda root, args: None)
    assert gitutil.fingerprint(Path(".")) == "nogit"


def test_fingerprint_head_failure(monkeypatch) -> None:
    """`HEAD` 不可解析：指纹固定为 `nogit`。"""
    fake = subprocess.CompletedProcess([], returncode=128, stdout="", stderr="")
    monkeypatch.setattr(gitutil, "_run", lambda root, args: fake)
    assert gitutil.fingerprint(Path(".")) == "nogit"


def test_fingerprint_tracks_content(git_repo: Path) -> None:
    """指纹随文件内容变化（未提交改动同样生效）。"""
    first = gitutil.fingerprint(git_repo)
    assert first == gitutil.fingerprint(git_repo)
    (git_repo / "docs" / "文档首页.md").write_text("# 改了内容\n", encoding="utf-8")
    assert gitutil.fingerprint(git_repo) != first


def test_fingerprint_with_deleted_file(git_repo: Path) -> None:
    """已删除的跟踪文件按不可读占位参与指纹（不抛错）。"""
    (git_repo / "docs" / "规范" / "规范.md").unlink()
    assert gitutil.fingerprint(git_repo) != "nogit"


def test_changed_services() -> None:
    """变更集里直接命中的工程名（去重保序）。"""
    changes = [
        "backend/services/svc/src/svc/app.py",
        "backend/services/svc/tests/test_app.py",
        "backend/libs/pkg_a/src/pkg_a/core.py",
        "backend/services/",
    ]
    assert gitutil.changed_services(changes, "backend", ("libs", "services")) == ["svc", "pkg_a"]
    assert gitutil.changed_services([], "backend", ("libs",)) == []
