"""命令行入口用例。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from engkit import cli


@pytest.fixture(autouse=True)
def _no_ci(monkeypatch):
    """默认非 CI、未包装（便于验证拦截路径）。"""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.delenv("ENGKIT_RUN_ONCE", raising=False)
    monkeypatch.delenv("BMS_RUN_ONCE", raising=False)
    monkeypatch.delenv("ENGKIT_ALLOW_UNWRAPPED", raising=False)
    monkeypatch.delenv("ENGKIT_CONFIG", raising=False)
    monkeypatch.setattr("engkit.guard._checked", False)


def test_links_ok(repo: Path, capsys) -> None:
    """链接校验通过。"""
    assert cli.main(["links", "--root", str(repo)]) == 0
    assert "链接自洽" in capsys.readouterr().out


def test_links_broken(build_repo, capsys) -> None:
    """存在断链即返回 1（并列前若干条）。"""
    root = build_repo(files={"docs/坏.md": "# 坏\n\n[a](无.md)\n"})
    assert cli.main(["links", "--root", str(root)]) == 1
    assert "[断链]" in capsys.readouterr().err


def test_links_quiet_missing_docs(build_repo, capsys) -> None:
    """文档根缺失返回 2（静默模式不列明细）。"""
    root = build_repo(config='[docs]\nroot = "缺失"\n')
    assert cli.main(["links", "--root", str(root), "--quiet"]) == 2
    assert "未找到文档根目录" in capsys.readouterr().err


def test_docs_scope_ok(repo: Path, capsys) -> None:
    """口径检查通过。"""
    assert cli.main(["docs-scope", "--root", str(repo)]) == 0
    assert "违规 0 处" in capsys.readouterr().out


def test_docs_scope_violation(build_repo, capsys) -> None:
    """存在违规即返回 1。"""
    root = build_repo(files={"docs/项目/X/任务/01/02.md": "# x\n\n本任务 4h\n"})
    assert cli.main(["docs-scope", "--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert "违规 1 处" in captured.out
    assert "进度类字段只允许" in captured.err


def test_docs_scope_self_test(repo: Path, capsys) -> None:
    """内置判定向量全通过。"""
    assert cli.main(["docs-scope", "--root", str(repo), "--self-test"]) == 0
    assert "全部通过" in capsys.readouterr().out


def test_docs_scope_self_test_mismatch(repo: Path, capsys) -> None:
    """白名单判定与预期不符即失败。"""
    import dataclasses

    from engkit import cli as cli_module

    def fake_whitelisted(rel, cfg):
        return True

    original = cli_module.docs_scope.is_whitelisted
    cli_module.docs_scope.is_whitelisted = fake_whitelisted
    try:
        cfg = cli_module._load(_namespace(repo))
        assert cli_module._scope_self_test(cfg) == 1
    finally:
        cli_module.docs_scope.is_whitelisted = original
    del dataclasses


def _namespace(repo: Path):
    """构造最小参数命名空间。

    Args:
        repo: 仓库根。

    Returns:
        object: 命名空间。
    """
    import argparse

    return argparse.Namespace(root=str(repo), config="")


def test_docs_scope_missing_root(build_repo, capsys) -> None:
    """文档根缺失返回 2。"""
    root = build_repo(config='[docs]\nroot = "缺失"\n')
    assert cli.main(["docs-scope", "--root", str(root)]) == 2
    assert "未找到文档根目录" in capsys.readouterr().err


def test_selection_json(repo: Path, capsys) -> None:
    """后端选测 JSON 输出。"""
    code = cli.main(["selection", "--root", str(repo), "--changed-file", "backend/libs/pkg_a/src/pkg_a/util.py"])
    assert code == 0
    assert '"cov_modules"' in capsys.readouterr().out


def test_selection_formats(repo: Path, capsys) -> None:
    """其余输出格式。"""
    base = ["selection", "--root", str(repo), "--changed-file", "backend/libs/pkg_a/src/pkg_a/util.py"]
    assert cli.main([*base, "--format", "pytest-args"]) == 0
    assert "libs/pkg_a/tests" in capsys.readouterr().out
    assert cli.main([*base, "--format", "cov-args"]) == 0
    assert "--cov=pkg_a.util" in capsys.readouterr().out
    assert cli.main([*base, "--format", "projects"]) == 0
    assert capsys.readouterr().out.strip() == "pkg_a svc"


def test_selection_frontend(repo: Path, capsys) -> None:
    """前端包推导（packages / json）。"""
    base = ["selection", "--root", str(repo), "--scope", "frontend", "--changed-file", "frontend/packages/core/a.ts"]
    assert cli.main([*base, "--format", "packages"]) == 0
    assert capsys.readouterr().out.strip() == "@mini/core @mini/desktop"
    assert cli.main(base) == 0
    assert '"packages"' in capsys.readouterr().out


def test_selection_changed_services(repo: Path, capsys) -> None:
    """直接变更的服务名。"""
    code = cli.main(
        [
            "selection",
            "--root",
            str(repo),
            "--changed-file",
            "backend/services/svc/src/svc/app.py",
            "--format",
            "changed-services",
        ]
    )
    assert code == 0
    assert "svc" in capsys.readouterr().out


def test_selection_bad_ref(repo: Path, capsys) -> None:
    """基准不可解析返回 1。"""
    assert cli.main(["selection", "--root", str(repo), "--changed-from", "nope"]) == 1
    assert "变更基准不可解析" in capsys.readouterr().err


def test_selection_without_backend(tmp_path: Path, capsys) -> None:
    """未配置后端时后端推导返回 1。"""
    root = tmp_path / "r"
    root.mkdir()
    (root / "engkit.toml").write_text('[docs]\nroot = "docs"\n', encoding="utf-8")
    (root / "docs").mkdir()
    assert cli.main(["selection", "--root", str(root), "--changed-file", "x.py"]) == 1
    capsys.readouterr()
    assert cli.main(["selection", "--root", str(root), "--changed-file", "x.py", "--format", "changed-services"]) == 0


def test_run_selected_command(repo: Path, capsys) -> None:
    """定向执行器：无受影响测试即 0。"""
    assert cli.main(["run-selected", "--root", str(repo), "--changed-file", "docs/文档首页.md"]) == 0
    assert "跳过 pytest" in capsys.readouterr().out


def test_coverage_command(tmp_path: Path, capsys) -> None:
    """覆盖率门禁：无数据返回 1。"""
    code = cli.main(["coverage", "--data-file", str(tmp_path / ".coverage"), "--out", str(tmp_path / "r.json")])
    assert code == 1
    assert "未采集到覆盖率数据" in capsys.readouterr().err


def test_preflight_self_test(repo: Path, capsys) -> None:
    """预检引擎自检。"""
    assert cli.main(["preflight", "--root", str(repo), "--self-test"]) == 0
    assert "全部通过" in capsys.readouterr().out


def test_preflight_blocked(repo: Path, capsys) -> None:
    """未包装即拦截。"""
    assert cli.main(["preflight", "--root", str(repo), "--fast"]) == 1
    assert "重命令守卫" in capsys.readouterr().err


def test_preflight_ci(repo: Path, monkeypatch, capsys) -> None:
    """CI 环境放行并执行快速档。"""
    monkeypatch.setenv("CI", "1")
    assert cli.main(["preflight", "--root", str(repo), "--fast", "--jobs", "off"]) == 0
    assert "档位：快速" in capsys.readouterr().out


def test_run_once(repo: Path, capsys) -> None:
    """重命令包装：缺命令 / 正常执行 / 只看缓存。"""
    assert cli.main(["run_once", "--root", str(repo)]) == 2
    assert "缺少命令" in capsys.readouterr().err
    cmd = [sys.executable, "-c", "print('wrapped')"]
    assert cli.main(["run_once", "--root", str(repo), "--", *cmd]) == 0
    assert "wrapped" in capsys.readouterr().out
    assert cli.main(["run_once", "--root", str(repo), "--show", "--", *cmd]) == 0
    assert "缓存命中" in capsys.readouterr().out


def test_guard_check(repo: Path, monkeypatch, capsys) -> None:
    """拦截判定：放行 / 拦截 / 缺命令。"""
    assert cli.main(["guard", "check", "--root", str(repo), "--", "pytest"]) == 1
    assert "重命令守卫" in capsys.readouterr().err
    monkeypatch.setenv("CI", "1")
    assert cli.main(["guard", "check", "--root", str(repo), "--", "pytest"]) == 0
    assert "放行" in capsys.readouterr().out
    assert cli.main(["guard", "check", "--root", str(repo)]) == 2


def test_guard_hooks(repo: Path, tmp_path: Path, capsys) -> None:
    """钩子安装 / 校验 / 卸载。"""
    venv = repo / ".venv"
    (venv / "Lib" / "site-packages").mkdir(parents=True)
    assert cli.main(["guard", "install-hook", "--root", str(repo)]) == 0
    assert "已安装" in capsys.readouterr().out
    assert cli.main(["guard", "check-hook", "--root", str(repo)]) == 0
    assert "正常" in capsys.readouterr().out
    assert cli.main(["guard", "remove-hook", "--root", str(repo)]) == 0
    assert "已卸载" in capsys.readouterr().out


def test_guard_hook_missing_venv(repo: Path, capsys) -> None:
    """找不到 site-packages 即返回 1。"""
    assert cli.main(["guard", "install-hook", "--root", str(repo)]) == 1
    assert "未找到 venv" in capsys.readouterr().err


def test_kiwi_dry_run(repo: Path, tmp_path: Path, capsys) -> None:
    """用例登记 dry-run 不连远端。"""
    cases = tmp_path / "cases.toml"
    cases.write_text('[[case]]\nsummary = "s"\ncategory = "c"\n', encoding="utf-8")
    assert cli.main(["kiwi", "register", "--root", str(repo), "--cases", str(cases), "--dry-run"]) == 0
    assert "待登记 1 条" in capsys.readouterr().out


def test_config_error(tmp_path: Path, capsys) -> None:
    """配置缺失返回 2。"""
    assert cli.main(["links", "--root", str(tmp_path)]) == 2
    assert "未找到 engkit.toml" in capsys.readouterr().err


def test_version(capsys) -> None:
    """`--version` 打印版本。"""
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert cli.VERSION in capsys.readouterr().out


def test_run_changed_dispatch(repo: Path, monkeypatch) -> None:
    """`run-changed` 转发参数（含并行档、超时与开关）。"""
    seen: dict[str, object] = {}

    def fake(cfg, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli.devloop, "run_changed", fake)
    code = cli.main(
        [
            "run-changed",
            "--root",
            str(repo),
            "--changed-file",
            "a.py",
            "--jobs",
            "2",
            "--timeout",
            "5",
            "--no-lint",
            "--no-frontend",
        ]
    )
    assert code == 0
    assert seen["changed_files"] == ("a.py",)
    assert seen["jobs"] == "2"
    assert seen["timeout"] == 5.0
    assert seen["lint"] is False
    assert seen["frontend"] is False


def test_selection_depth_option(repo: Path, monkeypatch) -> None:
    """`selection --depth` 透传给选测。"""
    seen: dict[str, object] = {}

    def fake(cfg, changed, **kwargs):
        seen.update(kwargs)
        return cli.selection.Selection()

    monkeypatch.setattr(cli.selection, "build_selection", fake)
    assert cli.main(["selection", "--root", str(repo), "--depth", "direct", "--changed-file", "a.py"]) == 0
    assert seen["depth"] == "direct"


def test_base_product_literals_cli(repo: Path, capsys) -> None:
    """`base product-literals` 走 CLI 分派：命中退 1，自检退 0。"""
    toml = repo / "engkit.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8")
        + '\n[rules.product_literals]\ndeny_globs = ["backend/**/*.py"]\nliterals = ["alpha"]\n',
        encoding="utf-8",
    )
    target = repo / "backend/services/svc/src/svc/tag.py"
    target.write_text('X = "alpha"\n', encoding="utf-8")
    assert cli.main(["base", "product-literals", "--root", str(repo)]) == 1
    assert "check_product_literals" in capsys.readouterr().out
    assert cli.main(["base", "product-literals", "--root", str(repo), "--self-test"]) == 0
