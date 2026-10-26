"""行尾护栏命令行用例。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engkit import cli

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
"""提交身份（避免依赖全局 git 配置）。"""


def _git(root: Path, *args: str) -> None:
    """在仓库内执行 git 命令。

    Args:
        root: 仓库根。
        *args: git 参数。
    """
    import os

    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env={**GIT_ENV, "PATH": os.environ.get("PATH", "")},
    )


@pytest.fixture(autouse=True)
def _no_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """绕开重命令拦截（本组用例只验证行尾判定）。"""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.delenv("ENGKIT_RUN_ONCE", raising=False)
    monkeypatch.setattr("engkit.guard._checked", False)


@pytest.fixture
def eol_repo(tmp_path: Path) -> Path:
    """造一个带 .gitattributes 的已提交仓库。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        Path: 仓库根。
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n*.bat text eol=crlf\n")
    (root / "bad.md").write_bytes(b"# bad\n")
    (root / "keep.bat").write_bytes(b"@echo off\n")
    (root / "engkit.toml").write_bytes(b'[repo]\nname = "t"\n\n[docs]\nroot = "docs"\n')
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_eol_ok(eol_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """全为 LF 时通过。"""
    assert cli.main(["eol", "--root", str(eol_repo)]) == 0
    assert "行尾违规 0 个" in capsys.readouterr().out


def test_eol_violation(eol_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """残留 CRLF 返回 1 并列出文件。"""
    (eol_repo / "bad.md").write_bytes(b"# bad\r\n")
    assert cli.main(["eol", "--root", str(eol_repo)]) == 1
    assert "bad.md" in capsys.readouterr().err


def test_eol_quiet(eol_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--quiet 只打印结论行。"""
    (eol_repo / "bad.md").write_bytes(b"# bad\r\n")
    assert cli.main(["eol", "--root", str(eol_repo), "--quiet"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "行尾违规 1 个" in captured.out


def test_eol_fix(eol_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--fix 就地改回 LF 并返回 0。"""
    (eol_repo / "bad.md").write_bytes(b"# bad\r\n")
    (eol_repo / "keep.bat").write_bytes(b"@echo off\r\n")
    assert cli.main(["eol", "--root", str(eol_repo), "--fix"]) == 0
    assert "已把 1 个文件改回 LF" in capsys.readouterr().out
    assert (eol_repo / "bad.md").read_bytes() == b"# bad\n"
    assert (eol_repo / "keep.bat").read_bytes() == b"@echo off\r\n"


def test_eol_fix_without_findings(eol_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """无违规时 --fix 走普通结论分支。"""
    assert cli.main(["eol", "--root", str(eol_repo), "--fix"]) == 0
    assert "行尾违规 0 个" in capsys.readouterr().out


def test_eol_skipped(monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """无 git 时打印跳过说明并返回 0。"""
    from engkit import eol

    monkeypatch.setattr(eol, "_git", lambda root, args: None)
    assert cli.main(["eol", "--root", str(repo)]) == 0
    assert "跳过" in capsys.readouterr().out


def test_eol_truncates_long_list(eol_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """违规超过 50 条时只列前 50 条并给出省略提示。"""
    from engkit import eol as eol_mod

    findings = tuple(eol_mod.EolFinding(file=f"f{index}.md", actual="crlf", expected="lf") for index in range(51))
    report = eol_mod.EolReport(files=51, findings=findings)
    monkeypatch_result = report

    import engkit.cli as cli_mod

    original = cli_mod.eol.check
    cli_mod.eol.check = lambda root, cfg: monkeypatch_result  # type: ignore[assignment]
    try:
        assert cli_mod.main(["eol", "--root", str(eol_repo)]) == 1
    finally:
        cli_mod.eol.check = original  # type: ignore[assignment]
    captured = capsys.readouterr()
    assert "其余 1 个从略" in captured.err
    assert "f50.md" not in captured.err
