"""Kiwi 用例登记用例（远端执行以替身注入，不触网）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engkit import kiwi

CASES = """
[[case]]
summary = "13_05 engkit"
category = "平台骨架"
priority = "P2"
status = "CONFIRMED"
tags = ["自动化", "工具"]
text = "覆盖范围与自动化文件路径"

[[case]]
summary = "第二条"
category = "平台骨架"
"""


def _cases_file(tmp_path: Path, body: str = CASES) -> Path:
    """写用例清单文件。

    Args:
        tmp_path: 临时目录。
        body: 清单正文。

    Returns:
        Path: 清单路径。
    """
    path = tmp_path / "cases.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_cases(tmp_path: Path) -> None:
    """清单解析（缺省值与标签）。"""
    cases = kiwi.load_cases(_cases_file(tmp_path))
    assert len(cases) == 2
    assert cases[0].tags == ("自动化", "工具")
    assert cases[1].priority == "P2"
    assert cases[1].status == "CONFIRMED"
    assert cases[1].tags == ("自动化",)


def test_load_cases_errors(tmp_path: Path) -> None:
    """清单异常分支。"""
    with pytest.raises(kiwi.KiwiError, match="读取用例清单失败"):
        kiwi.load_cases(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[[case\n", encoding="utf-8")
    with pytest.raises(kiwi.KiwiError, match="解析失败"):
        kiwi.load_cases(bad)
    empty = tmp_path / "empty.toml"
    empty.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(kiwi.KiwiError, match="缺少"):
        kiwi.load_cases(empty)


def test_load_cases_invalid_entries(tmp_path: Path) -> None:
    """条目不是表 / 缺字段 / tags 非字符串数组。"""
    not_table = tmp_path / "a.toml"
    not_table.write_text("case = [1]\n", encoding="utf-8")
    with pytest.raises(kiwi.KiwiError, match="不是表"):
        kiwi.load_cases(not_table)
    missing = tmp_path / "b.toml"
    missing.write_text('[[case]]\nsummary = "x"\n', encoding="utf-8")
    with pytest.raises(kiwi.KiwiError, match="缺少 summary"):
        kiwi.load_cases(missing)
    bad_tags = tmp_path / "c.toml"
    bad_tags.write_text('[[case]]\nsummary = "x"\ncategory = "c"\ntags = [1]\n', encoding="utf-8")
    with pytest.raises(kiwi.KiwiError, match="tags"):
        kiwi.load_cases(bad_tags)


def test_read_env_file(tmp_path: Path) -> None:
    """凭据文件解析（注释与无等号行跳过）。"""
    assert kiwi.read_env_file(tmp_path / "none") == {}
    path = tmp_path / ".env"
    path.write_text("# 注释\n\nA=1\nBADLINE\nB = two \n", encoding="utf-8")
    assert kiwi.read_env_file(path) == {"A": "1", "B": "two"}


def test_credentials_from_file(repo: Path, cfg, monkeypatch) -> None:
    """凭据取自文件（环境变量优先）。"""
    monkeypatch.delenv("MINI_HOST", raising=False)
    monkeypatch.delenv("MINI_USER", raising=False)
    assert kiwi.credentials(cfg) == {"host": "10.0.0.1", "user": "ops"}
    monkeypatch.setenv("MINI_HOST", "1.2.3.4")
    assert kiwi.credentials(cfg)["host"] == "1.2.3.4"


def test_credentials_missing(cfg, monkeypatch) -> None:
    """缺凭据时报错（提示凭据不入库）。"""
    monkeypatch.delenv("MINI_HOST", raising=False)
    monkeypatch.delenv("MINI_USER", raising=False)
    (cfg.root / "deploy" / ".env").unlink()
    with pytest.raises(kiwi.KiwiError, match="凭据不入库"):
        kiwi.credentials(cfg)


def test_ssh_target_and_argv(cfg, monkeypatch) -> None:
    """SSH 目标串与远端命令。"""
    monkeypatch.delenv("MINI_HOST", raising=False)
    monkeypatch.delenv("MINI_USER", raising=False)
    assert kiwi.ssh_target(cfg) == "ops@10.0.0.1"
    assert kiwi.remote_argv(cfg)[:2] == ["ssh", "ops@10.0.0.1"]
    assert "mini-kiwi" in kiwi.remote_argv(cfg)[2]


def test_default_runner(cfg, monkeypatch, tmp_path: Path) -> None:
    """真实执行器：脚本经 stdin 传入并合并输出。"""
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "out", "err")

    monkeypatch.setattr(kiwi.subprocess, "run", fake_run)
    result = kiwi.default_runner(cfg, tmp_path, "print(1)\n")
    assert result.code == 0
    assert result.output == "outerr"
    assert captured["input"] == "print(1)\n"


def test_default_runner_oserror(cfg, monkeypatch, tmp_path: Path) -> None:
    """SSH 不可执行时报错。"""

    def fake_run(*args, **kwargs):
        raise OSError("no ssh")

    monkeypatch.setattr(kiwi.subprocess, "run", fake_run)
    with pytest.raises(kiwi.KiwiError, match="无法执行 ssh"):
        kiwi.default_runner(cfg, tmp_path, "")


def test_scripts(tmp_path: Path) -> None:
    """脚本生成内容。"""
    cases = kiwi.load_cases(_cases_file(tmp_path))
    script = kiwi.register_script(cases, "admin")
    assert "TestCase.objects.create" in script
    assert "['自动化', '工具']" in script
    assert "created total" in script
    assert "order_by('-pk')" in kiwi.list_script(5)
    assert "engkit-kiwi-ok" in kiwi.ping_script()


def test_register_dry_run(tmp_path: Path, cfg, capsys) -> None:
    """`--dry-run` 不连远端。"""
    assert kiwi.register(cfg, _cases_file(tmp_path), dry_run=True) == 0
    assert "待登记 2 条" in capsys.readouterr().out


def test_register_success(tmp_path: Path, cfg, capsys) -> None:
    """登记成功并回读校验。"""
    calls: list[str] = []

    def runner(config, cwd, script):
        calls.append(script)
        return kiwi.RemoteResult(code=0, output="created case id = 7\ncreated total = 2\n")

    assert kiwi.register(cfg, _cases_file(tmp_path), runner=runner) == 0
    assert "登记完成：2 条" in capsys.readouterr().out
    assert calls


def test_register_mismatch(tmp_path: Path, cfg, capsys) -> None:
    """回读不一致即失败。"""
    runner = lambda *args: kiwi.RemoteResult(code=0, output="created total = 1\n")  # noqa: E731
    assert kiwi.register(cfg, _cases_file(tmp_path), runner=runner) == 1
    assert "回读不一致" in capsys.readouterr().err


def test_register_remote_failure(tmp_path: Path, cfg, capsys) -> None:
    """远端失败即返回 1。"""
    runner = lambda *args: kiwi.RemoteResult(code=2, output="boom\n")  # noqa: E731
    assert kiwi.register(cfg, _cases_file(tmp_path), runner=runner) == 1
    assert "登记失败" in capsys.readouterr().err


def test_register_bad_cases(tmp_path: Path, cfg, capsys) -> None:
    """清单异常返回 2。"""
    assert kiwi.register(cfg, tmp_path / "missing.toml") == 2
    assert "读取用例清单失败" in capsys.readouterr().err


def test_register_credentials_error(tmp_path: Path, cfg, capsys) -> None:
    """凭据缺失返回 2。"""

    def runner(*args):
        raise kiwi.KiwiError("缺凭据")

    assert kiwi.register(cfg, _cases_file(tmp_path), runner=runner) == 2
    assert "缺凭据" in capsys.readouterr().err


def test_list_cases(cfg, capsys) -> None:
    """回读：成功 / 远端失败 / 凭据缺失。"""
    assert kiwi.list_cases(cfg, runner=lambda *args: kiwi.RemoteResult(0, "[(1, 'x')]")) == 0
    assert "[(1, 'x')]" in capsys.readouterr().out
    assert kiwi.list_cases(cfg, runner=lambda *args: kiwi.RemoteResult(1, "")) == 1
    capsys.readouterr()

    def broken(*args):
        raise kiwi.KiwiError("缺凭据")

    assert kiwi.list_cases(cfg, runner=broken) == 2
    assert "缺凭据" in capsys.readouterr().err


def test_ping(cfg, capsys) -> None:
    """连通性探测三态。"""
    assert kiwi.ping(cfg, runner=lambda *args: kiwi.RemoteResult(0, "engkit-kiwi-ok\n")) == 0
    assert "可达" in capsys.readouterr().out
    assert kiwi.ping(cfg, runner=lambda *args: kiwi.RemoteResult(0, "nothing\n")) == 1
    assert "不可达" in capsys.readouterr().err

    def broken(*args):
        raise kiwi.KiwiError("缺凭据")

    assert kiwi.ping(cfg, runner=broken) == 2
    assert "缺凭据" in capsys.readouterr().err
