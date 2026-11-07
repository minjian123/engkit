"""`engkit checks` 子命令用例（Kiwi 2446）：分派、空清单、失败退出码与 `--only` 透传。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import BASE_CONFIG

from engkit import cli

pytestmark = pytest.mark.kiwi_id(2446)


def _without_self_tests() -> str:
    """取一份未登记任何护栏检查的配置（只保留 `self_tests = []` 之前的段）。

    Returns:
        str: 配置文本。
    """
    return BASE_CONFIG.split("self_tests = [")[0] + "self_tests = []" + chr(10)


def test_checks_cli_empty_list(build_repo, capsys) -> None:
    """未登记任何检查时返回 0 并提示。"""
    root = build_repo(config=_without_self_tests())
    assert cli.main(["checks", "--root", str(root)]) == 0
    assert "未登记任何检查" in capsys.readouterr().out


def test_checks_cli_runs_registered(repo: Path, capsys) -> None:
    """按登记表执行并输出汇总（缺省夹具有一条自测）。"""
    assert cli.main(["checks", "--root", str(repo)]) == 0
    assert "通过 1 / 失败 0" in capsys.readouterr().out


def test_checks_cli_failure_exit_code(repo: Path, monkeypatch) -> None:
    """有失败项时返回 1，`--only` 透传给 runner。"""
    seen: dict[str, object] = {}

    def fake(cfg, entries, *, changed=(), only=()):
        seen["only"] = tuple(only)
        return ["甲"]

    monkeypatch.setattr(cli.checks, "run_batch", fake)
    assert cli.main(["checks", "--root", str(repo), "--only", "甲"]) == 1
    assert seen["only"] == ("甲",)
