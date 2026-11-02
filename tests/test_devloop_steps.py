"""开发环附加步骤（`[[devloop.steps]]`）用例：解析、占位展开、条件跳过与失败即停。

口径：附加步骤用于把「秒级护栏」放进开发环（如裸集合 / 产品字面量的增量扫描、卫生、pyright），
由仓侧 `engkit.toml` 声明命令模板；命令失败即停，不再往下跑测试。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from engkit import devloop
from engkit.config import Config, ConfigError, DevloopStep, parse


def _py(code: str) -> tuple[str, ...]:
    """构造「用当前解释器执行一段代码」的命令模板。

    Args:
        code: Python 代码。

    Returns:
        tuple[str, ...]: 命令模板。
    """
    return ("{python}", "-c", code)


def _cfg_with(repo: Path, cfg: Config, *steps: DevloopStep) -> Config:
    """给配置注入附加步骤。

    Args:
        repo: 仓库根（未使用，保留以强调步骤命令按仓根 cwd 执行）。
        cfg: 原配置。
        steps: 步骤。

    Returns:
        Config: 注入后的配置。
    """
    del repo
    return dataclasses.replace(cfg, devloop_steps=steps)


# ---------------- 解析 ----------------


def test_parse_devloop_steps_ok() -> None:
    """正常解析：label / cmd / only_if（缺省空串）。"""
    raw = {
        "devloop": {
            "steps": [
                {"label": "护栏", "cmd": ["{python}", "x.py", "{changed}"]},
                {"label": "静态", "cmd": ["uv", "run", "pyright", "{changed_py*}"], "only_if": "changed_py"},
            ]
        }
    }
    steps = parse({"docs": {"root": "docs"}, **raw}, Path("engkit.toml")).devloop_steps
    assert [step.label for step in steps] == ["护栏", "静态"]
    assert steps[0].only_if == ""
    assert steps[1].cmd[-1] == "{changed_py*}"


def test_parse_devloop_steps_absent() -> None:
    """未声明 `[[devloop.steps]]`：空表（开发环行为不变）。"""
    assert parse({"docs": {"root": "docs"}}, Path("engkit.toml")).devloop_steps == ()


@pytest.mark.parametrize(
    "section",
    [
        {"devloop": []},
        {"devloop": {"steps": "x"}},
        {"devloop": {"steps": [1]}},
        {"devloop": {"steps": [{"cmd": ["x"]}]}},
        {"devloop": {"steps": [{"label": " ", "cmd": ["x"]}]}},
        {"devloop": {"steps": [{"label": "x", "cmd": []}]}},
        {"devloop": {"steps": [{"label": "x", "cmd": [1]}]}},
        {"devloop": {"steps": [{"label": "x", "cmd": ["y"], "only_if": "always"}]}},
    ],
)
def test_parse_devloop_steps_invalid(section: dict) -> None:
    """结构非法即拒：非表 / steps 非数组 / 项非表 / 缺 label / cmd 空或非字符串 / only_if 越界。"""
    with pytest.raises(ConfigError):
        parse({"docs": {"root": "docs"}, **section}, Path("engkit.toml"))


# ---------------- 执行 ----------------


def test_run_steps_without_config(repo: Path, cfg: Config) -> None:
    """未配置步骤：直接通过（不产生任何命令）。"""
    assert devloop._run_steps(cfg, ["a.py"]) == 0


def test_run_steps_stop_on_failure(repo: Path, cfg: Config) -> None:
    """失败即停：第二个步骤失败后不再执行第三个。"""
    config = _cfg_with(
        repo,
        cfg,
        DevloopStep(label="一", cmd=_py("from pathlib import Path; Path('one.txt').write_text('1')")),
        DevloopStep(label="二", cmd=_py("raise SystemExit(3)")),
        DevloopStep(label="三", cmd=_py("from pathlib import Path; Path('three.txt').write_text('3')")),
    )
    assert devloop._run_steps(config, ["a.py"]) == 3
    assert (repo / "one.txt").exists()
    assert not (repo / "three.txt").exists()


def test_run_steps_skip_without_changed_py(repo: Path, cfg: Config) -> None:
    """`only_if = changed_py`：本次无改动 `.py` 时跳过该步骤。"""
    config = _cfg_with(
        repo,
        cfg,
        DevloopStep(
            label="静态",
            cmd=_py("from pathlib import Path; Path('ran.txt').write_text('1')"),
            only_if="changed_py",
        ),
    )
    assert devloop._run_steps(config, ["docs/a.md"]) == 0
    assert not (repo / "ran.txt").exists()


def test_run_steps_skip_when_py_placeholder_unused(repo: Path, cfg: Config) -> None:
    """命令含 `{changed_py*}` 但无改动 `.py`：跳过（否则占位展开为空会退回全量扫描）。"""
    config = _cfg_with(
        repo,
        cfg,
        DevloopStep(
            label="类型",
            cmd=(*_py("from pathlib import Path; Path('ran2.txt').write_text('1')"), "{changed_py*}"),
        ),
    )
    assert devloop._run_steps(config, ["docs/a.md"]) == 0
    assert not (repo / "ran2.txt").exists()


def test_run_steps_placeholders(repo: Path, cfg: Config) -> None:
    """占位展开：`{changed}` 渲染成 `-` 并由管道承载清单、`{changed_py*}` 展开为多文件、`{root}` 生效。"""
    (repo / "a.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("Y = 2\n", encoding="utf-8")
    code = (
        "import sys, pathlib;"
        "names = sorted(p.replace(chr(92), '/').split('/')[-1] for p in sys.argv[3:]);"
        "pathlib.Path('dump.txt').write_text("
        "sys.argv[1] + '<<' + sys.stdin.read() + '|' + ','.join(names) + '|' + sys.argv[2])"
    )
    config = _cfg_with(
        repo,
        cfg,
        DevloopStep(label="占位", cmd=("{python}", "-c", code, "{changed}", "{root}", "{changed_py*}")),
    )
    assert devloop._run_steps(config, ["a.py", "b.py", "docs/x.md"]) == 0
    dumped = (repo / "dump.txt").read_text(encoding="utf-8")
    marker, _, rest = dumped.partition("<<")
    assert marker == devloop.STDIN_LIST
    assert not (repo / ".cache" / "devloop").exists()
    head, _, tail = rest.partition("|")
    assert head.splitlines() == ["a.py", "b.py", "docs/x.md"]
    assert tail.endswith("|" + str(repo))


def test_listing_text() -> None:
    """清单文本：一行一个相对路径、末尾带换行；空清单为空串。"""
    assert devloop._listing(["a.py", "docs/x.md"]) == "a.py\ndocs/x.md\n"
    assert devloop._listing([]) == ""


def test_expand_step_python_and_root(repo: Path, cfg: Config) -> None:
    """`{python}` 取当前解释器、`{root}` 取仓根、清单占位渲染成 `-` 并带出内容。"""
    argv, stdin_text = devloop._expand_step(
        cfg,
        DevloopStep(label="x", cmd=("{python}", "{root}", "{changed}")),
        changed=["a.py"],
        py_files=["a.py"],
    )
    assert argv[0] == sys.executable
    assert argv[1] == str(repo)
    assert argv[2] == devloop.STDIN_LIST
    assert stdin_text == "a.py\n"


def test_expand_step_py_list_and_conflict(repo: Path, cfg: Config) -> None:
    """`{changed_py}` 只带 `.py`；同一步骤混用两份清单直接拒绝（管道只能承载一份）。"""
    argv, stdin_text = devloop._expand_step(
        cfg,
        DevloopStep(label="x", cmd=("--files-from", "{changed_py}")),
        changed=["a.py", "docs/x.md"],
        py_files=["a.py"],
    )
    assert argv == ["--files-from", devloop.STDIN_LIST]
    assert stdin_text == "a.py\n"

    with pytest.raises(ValueError, match="标准输入只能承载一份清单"):
        devloop._expand_step(
            cfg,
            DevloopStep(label="y", cmd=("{changed}", "{changed_py}")),
            changed=["a.py"],
            py_files=["a.py"],
        )


def test_run_changed_steps_failure_skips_tests(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """附加步骤失败即停：不跑选测，返回步骤退出码。"""
    ran = []
    monkeypatch.setattr(devloop, "_lint_changed", lambda cfg, changed: 0)
    monkeypatch.setattr(devloop, "_run_steps", lambda cfg, changed: 4)
    monkeypatch.setattr(devloop.preflight, "run_selected", lambda *a, **k: ran.append("pytest") or 0)
    assert devloop.run_changed(cfg, changed_files=("a.py",)) == 4
    assert ran == []


def test_run_changed_steps_disabled(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """`steps=False`（`--no-steps`）：跳过附加步骤，直接进选测。"""
    ran = []
    monkeypatch.setattr(devloop, "_lint_changed", lambda cfg, changed: 0)
    monkeypatch.setattr(devloop, "_run_steps", lambda cfg, changed: ran.append("steps") or 0)
    monkeypatch.setattr(devloop.preflight, "run_selected", lambda *a, **k: 0)
    monkeypatch.setattr(devloop, "_frontend_changed", lambda cfg, changed: 0)
    assert devloop.run_changed(cfg, changed_files=("a.py",), steps=False) == 0
    assert ran == []


def test_run_steps_rejects_two_lists(repo: Path, cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    """同一步骤混用两份清单：拒绝执行并返回 2，不进入下一个步骤。"""
    (repo / "a.py").write_text("X = 1" + chr(10), encoding="utf-8")
    config = _cfg_with(repo, cfg, DevloopStep(label="混用", cmd=("{changed}", "{changed_py}")))
    assert devloop._run_steps(config, ["a.py"]) == 2
    assert "标准输入只能承载一份清单" in capsys.readouterr().err
