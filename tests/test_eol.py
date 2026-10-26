"""行尾护栏用例。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engkit import eol
from engkit.config import EolConfig, load

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
    # 一律用 write_bytes 显式写 LF：write_text 在 Windows 上默认写 CRLF，
    # 正是本护栏要防的那种「静默变 CRLF」，夹具自身不能踩。
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n*.bat text eol=crlf\n")
    (root / "ok.md").write_bytes(b"# ok\n")
    (root / "bad.md").write_bytes(b"# bad\n")
    (root / "keep.bat").write_bytes(b"@echo off\n")
    (root / "engkit.toml").write_bytes(b'[repo]\nname = "t"\n\n[docs]\nroot = "docs"\n')
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_parse_eol_line_lf() -> None:
    """解析 attr 段被空白切开的行（eol 在第二个 token）。"""
    parsed = eol._parse_eol_line("i/lf    w/crlf  attr/text=auto eol=lf \tbad.md")
    assert parsed == ("crlf", "lf", "bad.md")


def test_parse_eol_line_crlf_attr() -> None:
    """解析 eol=crlf 的属性。"""
    parsed = eol._parse_eol_line("i/lf    w/crlf  attr/text eol=crlf    \tkeep.bat")
    assert parsed == ("crlf", "crlf", "keep.bat")


def test_parse_eol_line_without_attr() -> None:
    """无属性时期望值为空串。"""
    parsed = eol._parse_eol_line("i/lf    w/lf    attr/                \ta.txt")
    assert parsed == ("lf", "", "a.txt")


def test_parse_eol_line_malformed() -> None:
    """缺制表符或无 w/ 字段时返回 None。"""
    assert eol._parse_eol_line("garbage") is None
    assert eol._parse_eol_line("i/lf  attr/text=auto\tx.txt") is None


def test_has_crlf(eol_repo: Path) -> None:
    """CRLF 判定按字节。"""
    assert eol.has_crlf(eol_repo / "ok.md") is False
    (eol_repo / "ok.md").write_bytes(b"# ok\r\n")
    assert eol.has_crlf(eol_repo / "ok.md") is True


def test_check_reports_crlf(eol_repo: Path) -> None:
    """应为 LF 却含 CRLF 的文件被报出。"""
    (eol_repo / "bad.md").write_bytes(b"# bad\r\n")
    report = eol.check(eol_repo, EolConfig())
    assert report.files == 5
    assert [item.file for item in report.findings] == ["bad.md"]
    assert report.findings[0].actual == "crlf"
    assert report.findings[0].expected == "lf"


def test_check_respects_crlf_exception(eol_repo: Path) -> None:
    """约定为 CRLF 的文件不报（*.bat）。"""
    (eol_repo / "keep.bat").write_bytes(b"@echo off\r\n")
    assert eol.check(eol_repo, EolConfig()).findings == ()


def test_check_skip_prefixes(eol_repo: Path) -> None:
    """skip_prefixes 内的路径不纳入检查。"""
    (eol_repo / "bad.md").write_bytes(b"# bad\r\n")
    assert eol.check(eol_repo, EolConfig(skip_prefixes=("bad",))).findings == ()


def test_check_include_suffixes(eol_repo: Path) -> None:
    """include_suffixes 只保留指定后缀。"""
    (eol_repo / "bad.md").write_bytes(b"# bad\r\n")
    report = eol.check(eol_repo, EolConfig(include_suffixes=(".bat",)))
    assert report.findings == ()
    assert report.files == 1


def test_check_without_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """git 不可用时跳过而非报错。

    直接让底层 git 调用返回 None（等价于环境无 git），比找「真不是仓库」的目录可靠：
    pytest 临时目录默认落在仓内，且部分环境下系统 Temp 不可写会让临时目录回退到仓内。
    """
    monkeypatch.setattr(eol, "_git", lambda root, args: None)
    report = eol.check(tmp_path, EolConfig())
    assert report.skipped is not None
    assert report.findings == ()


def test_check_git_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """git 返回非零退出码时同样跳过。"""

    class _Result:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(eol, "_git", lambda root, args: _Result())
    report = eol.check(tmp_path, EolConfig())
    assert report.skipped is not None


def test_normalize_writes_lf(eol_repo: Path) -> None:
    """normalize 把 CRLF 写回为 LF，且不动约定为 CRLF 的文件。"""
    (eol_repo / "bad.md").write_bytes(b"# bad\r\n")
    (eol_repo / "keep.bat").write_bytes(b"@echo off\r\n")
    report = eol.check(eol_repo, EolConfig())
    fixed = eol.normalize(eol_repo, report)
    assert fixed == ["bad.md"]
    assert (eol_repo / "bad.md").read_bytes() == b"# bad\n"
    assert (eol_repo / "keep.bat").read_bytes() == b"@echo off\r\n"


def test_normalize_missing_file(eol_repo: Path) -> None:
    """目标文件不可读时跳过。"""
    report = eol.EolReport(files=1, findings=(eol.EolFinding(file="gone.md", actual="crlf", expected="lf"),))
    assert eol.normalize(eol_repo, report) == []


def test_check_skips_unparsable_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """git 输出中出现无法解析的行时跳过该行（不中断检查）。"""

    class _Result:
        returncode = 0
        stdout = "garbage-line-without-tab\ni/lf    w/crlf  attr/text=auto eol=lf \tbad.md"

    monkeypatch.setattr(eol, "_git", lambda root, args: _Result())
    report = eol.check(tmp_path, EolConfig())
    assert [item.file for item in report.findings] == ["bad.md"]


def test_matches_without_filters() -> None:
    """未配置过滤时全部纳入。"""
    assert eol._matches("any/path.txt", EolConfig()) is True


def test_config_eol_section(tmp_path: Path) -> None:
    """[eol] 段可解析。"""
    root = tmp_path / "r"
    root.mkdir()
    (root / "engkit.toml").write_bytes(
        b'[repo]\nname = "t"\n\n[docs]\nroot = "docs"\n\n[eol]\nskip_prefixes = ["a/"]\ninclude_suffixes = [".py"]\n'
    )
    cfg = load(root / "engkit.toml")
    assert cfg.eol.skip_prefixes == ("a/",)
    assert cfg.eol.include_suffixes == (".py",)


def test_config_eol_defaults(tmp_path: Path) -> None:
    """未写 [eol] 时取默认值。"""
    root = tmp_path / "r2"
    root.mkdir()
    (root / "engkit.toml").write_bytes(b'[repo]\nname = "t"\n\n[docs]\nroot = "docs"\n')
    cfg = load(root / "engkit.toml")
    assert cfg.eol.skip_prefixes == ()
    assert cfg.eol.include_suffixes == ()
