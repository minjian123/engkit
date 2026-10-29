"""增量扫描（`--files-from`）用例：等价性、范围裁剪与参数冲突。

口径：裸集合与产品字面量的规则都是**文件局部**的，故「只扫改动文件」的新增判定必须与全量**等价**——
本文件即这条等价性的机器断言（同一改动分别按全量与增量跑，问题清单一致）。

覆盖：清单解析（注释 / 空行 / 反斜杠 / 重复 / 不可读）、范围裁剪（不存在 / 非目标扩展名 /
排除目录 / 不在扫描目标下）、`stale` 只统计已扫文件、`--update-baseline` 与增量互斥、
产品字面量的跨基线断言在增量下跳过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from base_fixtures import config, write

from engkit.base import bare_collections as bc
from engkit.base import product_literals as pl
from engkit.base import scan_scope
from engkit.config import parse

_BARE = "def f(x: list[int]) -> None:\n    del x\n"
"""一条裸集合违规（`list` 注解）。"""

_OTHER_BARE = "class A:\n    data: set[int] = set()\n"
"""另一条裸集合违规（`set` 注解）。"""


def _bare_cfg(tmp_path: Path):
    """造裸集合配置（扫描目标 `src` / `svc`）。

    Args:
        tmp_path: 临时仓根。

    Returns:
        object: 配置。
    """
    return config(tmp_path)


def _baseline(tmp_path: Path, entries: list[dict]) -> None:
    """写裸集合基线。

    Args:
        tmp_path: 临时仓根。
        entries: 基线条目。
    """
    (tmp_path / "baseline.json").write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


def _list(tmp_path: Path, name: str, lines: list[str]) -> Path:
    """写改动清单。

    Args:
        tmp_path: 临时仓根。
        name: 清单文件名。
        lines: 行内容。

    Returns:
        Path: 清单路径。
    """
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------- 清单解析 ----------------


def test_normalize_lines_strips_comments_and_duplicates() -> None:
    """清单规范化：去空行 / 注释 / 重复，反斜杠归一为 `/`，保留首次出现顺序。"""
    lines = ["", "  src/a.py  ", "# 注释", "src\\b.py", "src/a.py", "   "]
    assert scan_scope.normalize_lines(lines) == ["src/a.py", "src/b.py"]


def test_read_file_list_missing_reports_none(tmp_path: Path) -> None:
    """清单不可读：返回 None（调用方转退出码 2，不静默当全量）。"""
    assert scan_scope.read_file_list(tmp_path / "nope.txt") is None


def test_select_files_skips_absent(tmp_path: Path) -> None:
    """清单裁剪：只保留仓内真实存在的文件。"""
    write(tmp_path, {"src/a.py": _BARE})
    picked = scan_scope.select_files(tmp_path, ["src/a.py", "src/ghost.py"])
    assert [path.name for path in picked] == ["a.py"]


# ---------------- 裸集合：增量等价 ----------------


def test_bare_incremental_matches_full_on_same_change(tmp_path: Path) -> None:
    """等价性：同一改动分别按全量与增量跑，问题清单一致。"""
    cfg = _bare_cfg(tmp_path)
    write(tmp_path, {"src/a.py": _BARE, "src/b.py": _OTHER_BARE})
    _baseline(tmp_path, [])

    full = bc.check(cfg, cfg.collections_rules)
    inc = bc.check(cfg, cfg.collections_rules, changed=["src/a.py", "src/b.py"])

    assert [p.split("（")[0] for p in full.problems] == [p.split("（")[0] for p in inc.problems]
    assert full.counts["added"] == inc.counts["added"] == 2


def test_bare_incremental_only_reports_changed_file(tmp_path: Path) -> None:
    """增量只报清单内文件：未改动文件即便有新增违规也不进本次结论。"""
    cfg = _bare_cfg(tmp_path)
    write(tmp_path, {"src/a.py": _BARE, "src/b.py": _OTHER_BARE})
    _baseline(tmp_path, [])
    inc = bc.check(cfg, cfg.collections_rules, changed=["src/a.py"])
    assert len(inc.problems) == 1
    assert "src/a.py" in inc.problems[0]


def test_bare_incremental_respects_baseline(tmp_path: Path) -> None:
    """增量沿用基线：清单内文件已在基线中的条目不报新增。"""
    cfg = _bare_cfg(tmp_path)
    write(tmp_path, {"src/a.py": _BARE})
    _baseline(tmp_path, [{"file": "src/a.py", "line": "def f(x: list[int]) -> None:", "container": "list", "count": 1}])
    assert bc.check(cfg, cfg.collections_rules, changed=["src/a.py"]).problems == []


def test_bare_incremental_stale_only_counts_scanned(tmp_path: Path) -> None:
    """`stale` 只统计已扫文件：未扫文件留在基线里不算「可递减」，避免增量误报。"""
    cfg = _bare_cfg(tmp_path)
    write(tmp_path, {"src/a.py": _BARE})
    _baseline(
        tmp_path,
        [
            {"file": "src/a.py", "line": "def f(x: list[int]) -> None:", "container": "list", "count": 1},
            {"file": "src/old.py", "line": "def gone(x: set[int]) -> None:", "container": "set", "count": 1},
        ],
    )
    assert bc.check(cfg, cfg.collections_rules, changed=["src/a.py"]).counts["stale"] == 0
    assert bc.check(cfg, cfg.collections_rules).counts["stale"] == 1


def test_bare_incremental_scope_cut(tmp_path: Path) -> None:
    """范围裁剪：不存在文件 / 非 `.py` / 排除目录 / 不在扫描目标下的文件均不进扫描面。"""
    cfg = _bare_cfg(tmp_path)
    write(tmp_path, {"src/a.py": _BARE, "src/notes.md": "x: list[int]\n", "other/c.py": _BARE})
    (tmp_path / "src/__pycache__").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/__pycache__/d.py").write_text(_BARE, encoding="utf-8")
    picked = bc.iter_target_files(
        cfg,
        cfg.collections_rules,
        changed=["src/a.py", "src/notes.md", "other/c.py", "src/__pycache__/d.py", "src/ghost.py"],
    )
    assert [path.name for path in picked] == ["a.py"]


# ---------------- 裸集合：run 参数与冲突 ----------------


def test_bare_run_incremental_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--files-from`：增量通过 / 增量报违规 / 清单不可读 / 与 `--update-baseline` 冲突。"""
    cfg = _bare_cfg(tmp_path)
    write(tmp_path, {"src/a.py": "def ok(x: StableList[int]) -> None:\n    del x\n"})
    _baseline(tmp_path, [])
    listing = _list(tmp_path, "changed.txt", ["src/a.py", "# 注释", ""])
    assert (
        bc.run(
            cfg,
            json_output=False,
            report_mode=False,
            update_baseline=False,
            as_self_test=False,
            files_from=str(listing),
        )
        == 0
    )
    assert "增量模式" in capsys.readouterr().out

    write(tmp_path, {"src/a.py": _BARE})
    assert (
        bc.run(
            cfg,
            json_output=False,
            report_mode=False,
            update_baseline=False,
            as_self_test=False,
            files_from=str(listing),
        )
        == 1
    )

    assert (
        bc.run(
            cfg,
            json_output=False,
            report_mode=False,
            update_baseline=False,
            as_self_test=False,
            files_from=str(tmp_path / "missing.txt"),
        )
        == 2
    )

    assert (
        bc.run(
            cfg,
            json_output=False,
            report_mode=False,
            update_baseline=True,
            as_self_test=False,
            files_from=str(listing),
        )
        == 2
    )


def test_bare_run_incremental_relative_listing(tmp_path: Path) -> None:
    """清单路径可相对仓根（与 `--root` 一致的调用习惯）。"""
    cfg = _bare_cfg(tmp_path)
    write(tmp_path, {"src/a.py": "def ok(x: StableList[int]) -> None:\n    del x\n", "changed.txt": "src/a.py\n"})
    _baseline(tmp_path, [])
    assert (
        bc.run(
            cfg,
            json_output=False,
            report_mode=False,
            update_baseline=False,
            as_self_test=False,
            files_from="changed.txt",
        )
        == 0
    )


# ---------------- 产品字面量：增量 ----------------


def _pl_cfg(root: Path, **overrides: object):
    """造产品字面量配置。

    Args:
        root: 临时仓根。
        overrides: 规则覆盖项。

    Returns:
        object: 配置。
    """
    rules: dict = {"deny_globs": ["svc/**/*.py"], "literals": ["alpha"]}
    rules.update(overrides)
    return parse({"docs": {"root": "docs"}, "rules": {"product_literals": rules}}, root / "engkit.toml")


def test_product_literals_incremental_matches_full(tmp_path: Path) -> None:
    """等价性：同一改动在全量与增量下新增判定一致。"""
    cfg = _pl_cfg(tmp_path)
    write(tmp_path, {"svc/a.py": "# alpha 用法\nX = 1\n", "svc/b.py": "# alpha 再说一次\nY = 2\n"})
    full = pl.check(cfg, cfg.product_literals_rules)
    inc = pl.check(cfg, cfg.product_literals_rules, changed=["svc/a.py", "svc/b.py"])
    assert len(full.problems) == len(inc.problems) == 2


def test_product_literals_incremental_only_changed(tmp_path: Path) -> None:
    """增量只报清单内文件；未改文件不参与本次结论。"""
    cfg = _pl_cfg(tmp_path)
    write(tmp_path, {"svc/a.py": "# alpha\nX = 1\n", "svc/b.py": "# alpha\nY = 2\n"})
    inc = pl.check(cfg, cfg.product_literals_rules, changed=["svc/b.py"])
    assert len(inc.problems) == 1
    assert "svc/b.py" in inc.problems[0]


def test_product_literals_incremental_skips_ref_assertion(tmp_path: Path) -> None:
    """跨基线断言（`baseline_ref`）在增量下跳过：基线文件不由本次改动触碰，比较无信息量。"""
    cfg = _pl_cfg(tmp_path)
    write(tmp_path, {"svc/a.py": "# alpha\nX = 1\n"})
    baseline = tmp_path / "pl_baseline.json"
    baseline.write_text(
        json.dumps({"version": 1, "entries": [{"file": "svc/a.py", "fingerprint": "x", "count": 1}]}),
        encoding="utf-8",
    )
    cfg = _pl_cfg(tmp_path, baseline=str(baseline.name), baseline_ref="origin/main")
    inc = pl.check(cfg, cfg.product_literals_rules, changed=["svc/a.py"])
    assert not any("[基线]" in problem or "增长" in problem for problem in inc.problems)


def test_product_literals_run_incremental_conflicts(tmp_path: Path) -> None:
    """`--files-from` 与 `--update-baseline` 互斥；清单不可读退出码 2。"""
    cfg = _pl_cfg(tmp_path)
    write(tmp_path, {"svc/a.py": "# alpha\nX = 1\n"})
    listing = _list(tmp_path, "changed.txt", ["svc/a.py"])
    assert (
        pl.run(
            cfg,
            json_output=False,
            update_baseline=True,
            baseline_ref="",
            as_self_test=False,
            files_from=str(listing),
        )
        == 2
    )
    assert (
        pl.run(
            cfg,
            json_output=False,
            update_baseline=False,
            baseline_ref="",
            as_self_test=False,
            files_from=str(tmp_path / "missing.txt"),
        )
        == 2
    )


def test_product_literals_incremental_scope_cut(tmp_path: Path) -> None:
    """范围裁剪：非目标扩展名 / 不在禁入面的文件不进扫描面。"""
    cfg = _pl_cfg(tmp_path)
    write(tmp_path, {"svc/a.py": "# alpha\nX = 1\n", "svc/notes.md": "alpha\n", "other/b.py": "# alpha\n"})
    picked = pl.iter_target_files(cfg, cfg.product_literals_rules, changed=["svc/notes.md", "other/b.py", "svc/a.py"])
    assert [path.name for path in picked] == ["a.py"]


def test_product_literals_run_incremental_relative_listing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--files-from` 相对清单 + 增量通过路径（打印增量模式行）。"""
    cfg = _pl_cfg(tmp_path)
    write(tmp_path, {"svc/a.py": "X = 1\n", "changed.txt": "svc/a.py\n"})
    assert (
        pl.run(
            cfg,
            json_output=False,
            update_baseline=False,
            baseline_ref="",
            as_self_test=False,
            files_from="changed.txt",
        )
        == 0
    )
    assert "增量模式" in capsys.readouterr().out
