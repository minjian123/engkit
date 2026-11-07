"""护栏单进程 runner（Kiwi 2446）：隔离、异常、回退、复用。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import BASE_CONFIG, init_git

from engkit import checks
from engkit.config import SelfTest, load

pytestmark = pytest.mark.kiwi_id(2446)

OK = "import sys\nsys.exit(0)\n"
FAIL = "import sys\nsys.exit(3)\n"
BOOM = "raise ValueError('boom')\n"


def _entry(label: str, script: Path) -> SelfTest:
    """造一条「python <脚本>」形态的检查。

    Args:
        label: 标签。
        script: 脚本路径。

    Returns:
        SelfTest: 检查项。
    """
    return SelfTest(label=label, cmd=(sys.executable, str(script)))


def _write(root: Path, rel: str, content: str) -> Path:
    """写脚本文件。

    Args:
        root: 仓库根。
        rel: 相对路径。
        content: 内容。

    Returns:
        Path: 绝对路径。
    """
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_order_and_failure_isolation(build_repo: Any, tmp_path: Path) -> None:
    """单条失败不中断整批，末尾汇总失败项。"""
    root = build_repo()
    marker = tmp_path / "marker.txt"
    tail = f"open(r'{marker}', 'a', encoding='utf-8').write('x')\n"
    entries = [
        _entry("一", _write(root, "scripts/a.py", OK)),
        _entry("二", _write(root, "scripts/b.py", FAIL)),
        _entry("三", _write(root, "scripts/c.py", BOOM)),
        _entry("四", _write(root, "scripts/d.py", tail)),
    ]
    failures = checks.run_batch(load(root / "engkit.toml"), entries)
    assert failures == ["二", "三"]
    assert marker.read_text(encoding="utf-8") == "x"


def test_argv_and_env_restored(build_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """脚本改动 argv / 环境变量后必须恢复（不影响后续条目与自身进程）。"""
    root = build_repo()
    script = _write(
        root,
        "scripts/mutate.py",
        "import os, sys\n"
        "sys.argv = ['changed']\n"
        "sys.path.insert(0, '/nowhere')\n"
        "os.environ['ENGKIT_CHECKS_PROBE'] = 'leaked'\n"
        "sys.exit(0)\n",
    )
    monkeypatch.delenv("ENGKIT_CHECKS_PROBE", raising=False)
    before_argv = list(sys.argv)
    checks.run_batch(load(root / "engkit.toml"), [_entry("改环境", script)])
    assert sys.argv == before_argv
    assert "ENGKIT_CHECKS_PROBE" not in __import__("os").environ


def test_non_python_command_falls_back_to_process(build_repo: Any) -> None:
    """非「python 脚本」形态回退子进程执行（回退到执行，不是跳过）。"""
    root = build_repo()
    entry = SelfTest(label="内联", cmd=(sys.executable, "-c", "print('hi')"))
    assert checks.run_batch(load(root / "engkit.toml"), [entry]) == []


def test_missing_script_reports_reason(build_repo: Any) -> None:
    """脚本不存在时给出可读原因。"""
    root = build_repo()
    entry = SelfTest(label="缺失", cmd=(sys.executable, str(root / "scripts" / "none.py")))
    assert checks.run_batch(load(root / "engkit.toml"), [entry]) == ["缺失"]


def test_empty_command_reports_reason(build_repo: Any) -> None:
    """空命令记失败而不是抛异常。"""
    root = build_repo()
    assert checks.run_batch(load(root / "engkit.toml"), [SelfTest(label="空", cmd=())]) == ["空"]


def test_only_filter_and_empty(build_repo: Any) -> None:
    """`only` 过滤；过滤后为空时不执行任何条目。"""
    root = build_repo()
    entries = [_entry("甲", _write(root, "scripts/a.py", OK)), _entry("乙", _write(root, "scripts/b.py", FAIL))]
    cfg = load(root / "engkit.toml")
    assert checks.run_batch(cfg, entries, only=("甲",)) == []
    assert checks.run_batch(cfg, entries, only=("不存在",)) == []


def test_reuse_skips_second_run(build_repo: Any, tmp_path: Path) -> None:
    """同一输入第二次直接复用（脚本不再执行）。"""
    root = build_repo()
    init_git(root)
    marker = tmp_path / "runs.txt"
    script = _write(root, "scripts/count.py", f"open(r'{marker}', 'a', encoding='utf-8').write('x')\n")
    cfg = load(root / "engkit.toml")
    entries = [_entry("计数", script)]
    assert checks.run_batch(cfg, entries) == []
    assert marker.read_text(encoding="utf-8") == "x"
    assert checks.run_batch(cfg, entries) == []
    assert marker.read_text(encoding="utf-8") == "x"


def test_reuse_disabled_runs_every_time(build_repo: Any, tmp_path: Path) -> None:
    """关闭复用后每次都真跑。"""
    import dataclasses

    root = build_repo()
    init_git(root)
    marker = tmp_path / "runs2.txt"
    script = _write(root, "scripts/count2.py", f"open(r'{marker}', 'a', encoding='utf-8').write('x')\n")
    raw = load(root / "engkit.toml")
    cfg = dataclasses.replace(raw, preflight=dataclasses.replace(raw.preflight, reuse=False))
    entries = [_entry("计数", script)]
    checks.run_batch(cfg, entries)
    checks.run_batch(cfg, entries)
    assert marker.read_text(encoding="utf-8") == "xx"


def test_exit_with_message_code(build_repo: Any) -> None:
    """`sys.exit("文本")` 记失败并保留文本（非整数退出码）。"""
    root = build_repo()
    script = _write(root, "scripts/msg.py", "import sys\nsys.exit('bad-thing')\n")
    assert checks.run_batch(load(root / "engkit.toml"), [_entry("文本退出", script)]) == ["文本退出"]


def test_missing_binary_falls_back(build_repo: Any) -> None:
    """子进程回退通路遇到命令不存在 → 127。"""
    root = build_repo()
    entry = SelfTest(label="缺命令", cmd=("definitely-missing-binary-xyz", "arg"))
    assert checks.run_batch(load(root / "engkit.toml"), [entry]) == ["缺命令"]


def test_subprocess_oserror_reported(build_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """子进程启动抛 OSError 时记失败而不是崩掉整批。"""
    root = build_repo()

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("spawn failed")

    monkeypatch.setattr(checks.subprocess, "run", boom)
    entry = SelfTest(label="起不来", cmd=("any-binary",))
    assert checks.run_batch(load(root / "engkit.toml"), [entry]) == ["起不来"]


def test_script_as_first_token(build_repo: Any) -> None:
    """命令首 token 直接是脚本（不经解释器）也能同进程执行。"""
    root = build_repo()
    script = _write(root, "scripts/direct.py", OK)
    entry = SelfTest(label="直跑", cmd=(str(script), "--flag"))
    assert checks.run_batch(load(root / "engkit.toml"), [entry]) == []


def test_preflight_process_runner(build_repo: Any) -> None:
    """`checks_runner = process` 时走逐条子进程通路（保留旧行为）。"""
    from engkit import preflight

    config = BASE_CONFIG.replace("self_tests = [", 'checks_runner = "process"\nself_tests = [').replace(
        'deps = ["scripts/tools/guard.py"]', "deps = []"
    )
    root = build_repo(config=config)
    cfg = load(root / "engkit.toml")
    failures: list[str] = []
    preflight.run_self_tests(
        cfg, preflight.Budget(step_seconds=30.0, total_seconds=60.0), failures, force_all=True, mode="fast"
    )
    assert failures == []
