"""表声明索引（`TableIndex`）用例。

口径：索引只缓存**解析结果**，键为 `(mtime_ns, size)`；跨文件结论每次现算。
本文件要证明三件事——①命中与未命中的结果**恒等**于现扫；②文件一变即失效；
③缓存文件损坏 / 不可写 / 版本不符时**降级放行**（只掉速度，不改结论）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engkit.base import table_index as ti

DECL = '"""模型。"""\n\n\nclass A:\n    __tablename__ = "sys_a"\n'
"""合法表声明样本（行号 5）。"""

TWO = '"""两个表。"""\n\nX = 1\n\n\nclass A:\n    __tablename__ = "sys_a"\n\n\nclass B:\n    __tablename__ = "Sys_B"\n'
"""两处声明样本（第二个大写，验证小写归一与行号）。"""


def write(path: Path, text: str) -> Path:
    """写文件（父目录自动创建）。

    Args:
        path: 文件路径。
        text: 内容。

    Returns:
        Path: 文件路径。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def bump(path: Path, text: str) -> None:
    """改写文件并把 mtime 前推（避免同 tick 内改写命中旧条目）。

    Args:
        path: 文件路径。
        text: 新内容。
    """
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_declared_tables_parses_and_normalizes(tmp_path: Path) -> None:
    """解析口径：只取字符串常量赋值，表名小写，行号 1 基。"""
    path = write(tmp_path / "m.py", TWO)
    assert ti.declared_tables(path) == [(7, "sys_a"), (11, "sys_b")]


def test_declared_tables_skips_files_without_marker(tmp_path: Path) -> None:
    """无表声明标记的文件直接返回空表（不解析）。"""
    assert ti.declared_tables(write(tmp_path / "x.py", "VALUE = 1\n")) == []


def test_index_counts_hit_and_miss(tmp_path: Path) -> None:
    """首轮 miss 并落盘；同一进程内再取即 hit。"""
    path = write(tmp_path / "m.py", DECL)
    index = ti.TableIndex(tmp_path, tmp_path / ".cache" / "idx.json")
    assert index.declarations(path) == [(5, "sys_a")]
    assert (index.misses, index.hits) == (1, 0)
    assert index.declarations(path) == [(5, "sys_a")]
    assert (index.misses, index.hits) == (1, 1)
    index.save(prune=True)
    assert (tmp_path / ".cache" / "idx.json").is_file()


def test_index_hit_across_processes(tmp_path: Path) -> None:
    """跨进程：第二个实例读缓存即命中，结果与现扫一致。"""
    path = write(tmp_path / "m.py", TWO)
    cache = tmp_path / ".cache" / "idx.json"
    first = ti.TableIndex(tmp_path, cache)
    assert first.declarations(path) == ti.declared_tables(path)
    first.save(prune=True)
    second = ti.TableIndex(tmp_path, cache)
    assert second.declarations(path) == ti.declared_tables(path)
    assert (second.hits, second.misses) == (1, 0)


def test_index_invalidates_on_change(tmp_path: Path) -> None:
    """文件变了（内容与大小都变）即重新解析，不会返回旧表名。"""
    path = write(tmp_path / "m.py", DECL)
    cache = tmp_path / ".cache" / "idx.json"
    index = ti.TableIndex(tmp_path, cache)
    index.declarations(path)
    index.save(prune=True)
    bump(path, TWO)
    fresh = ti.TableIndex(tmp_path, cache)
    assert fresh.declarations(path) == [(7, "sys_a"), (11, "sys_b")]
    assert fresh.misses == 1


def test_index_cache_version_mismatch_is_ignored(tmp_path: Path) -> None:
    """版本不符即整体作废（解析口径变了不能信旧结果）。"""
    path = write(tmp_path / "m.py", DECL)
    cache = write(
        tmp_path / ".cache" / "idx.json",
        json.dumps({"version": ti.CACHE_VERSION + 1, "entries": {"m.py": [1, 1, [[1, "stale"]]]}}),
    )
    del cache
    index = ti.TableIndex(tmp_path, tmp_path / ".cache" / "idx.json")
    assert index.declarations(path) == [(5, "sys_a")]
    assert index.misses == 1


@pytest.mark.parametrize(
    "payload",
    [
        "{ broken",
        json.dumps([]),
        json.dumps({"version": ti.CACHE_VERSION}),
        json.dumps({"version": ti.CACHE_VERSION, "entries": {"m.py": "x"}}),
        json.dumps({"version": ti.CACHE_VERSION, "entries": {"m.py": [1, 2]}}),
        json.dumps({"version": ti.CACHE_VERSION, "entries": {"m.py": ["a", "b", []]}}),
        json.dumps({"version": ti.CACHE_VERSION, "entries": {"m.py": [1, 2, []]}}),
    ],
)
def test_index_tolerates_broken_cache(tmp_path: Path, payload: str) -> None:
    """缓存损坏 / 结构非法：当作空缓存继续跑（降级放行）。"""
    path = write(tmp_path / "m.py", DECL)
    write(tmp_path / ".cache" / "idx.json", payload)
    index = ti.TableIndex(tmp_path, tmp_path / ".cache" / "idx.json")
    assert index.declarations(path) == [(5, "sys_a")]


def test_index_prune_drops_unseen_entries(tmp_path: Path) -> None:
    """全量收尾按「本轮所见」剪枝：已删除文件的条目被清掉。"""
    keep = write(tmp_path / "keep.py", DECL)
    gone = write(tmp_path / "gone.py", DECL)
    cache = tmp_path / ".cache" / "idx.json"
    warm = ti.TableIndex(tmp_path, cache)
    warm.declarations(keep)
    warm.declarations(gone)
    warm.save(prune=True)
    gone.unlink()
    index = ti.TableIndex(tmp_path, cache)
    index.declarations(keep)
    index.save(prune=True)
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert list(saved["entries"]) == ["keep.py"]


def test_index_incremental_does_not_prune(tmp_path: Path) -> None:
    """增量模式不剪枝：没见全就剪会误删未扫文件的条目。"""
    keep = write(tmp_path / "keep.py", DECL)
    other = write(tmp_path / "other.py", DECL)
    cache = tmp_path / ".cache" / "idx.json"
    warm = ti.TableIndex(tmp_path, cache)
    warm.declarations(keep)
    warm.declarations(other)
    warm.save(prune=True)
    index = ti.TableIndex(tmp_path, cache)
    index.declarations(keep)
    index.save(prune=False)
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert sorted(saved["entries"]) == ["keep.py", "other.py"]


def test_index_without_cache_file_never_writes(tmp_path: Path) -> None:
    """未配置缓存路径（None）时只做进程内索引，不落盘。"""
    path = write(tmp_path / "m.py", DECL)
    index = ti.TableIndex(tmp_path, None)
    assert index.declarations(path) == [(5, "sys_a")]
    index.save(prune=True)
    assert not (tmp_path / ".cache").exists()


def test_index_save_failure_is_swallowed(tmp_path: Path) -> None:
    """缓存目录不可建（父路径是文件）时不抛错——只掉速度。"""
    blocker = write(tmp_path / "blocked", "x")
    path = write(tmp_path / "m.py", DECL)
    index = ti.TableIndex(tmp_path, blocker / "nested" / "idx.json")
    index.declarations(path)
    index.save(prune=True)


def test_index_missing_file_yields_no_declaration(tmp_path: Path) -> None:
    """枚举与读取之间文件被删：按无声明处理（下一次全量剪枝清条目）。"""
    index = ti.TableIndex(tmp_path, None)
    assert index.declarations(tmp_path / "not-there.py") == []


def test_index_relative_path_outside_root(tmp_path: Path) -> None:
    """仓根外的路径按绝对 posix 记键（不抛错，仍能索引）。"""
    outside = write(tmp_path.parent / "outside_m.py", DECL)
    index = ti.TableIndex(tmp_path, None)
    assert index.declarations(outside) == [(5, "sys_a")]


def test_index_save_skips_when_clean(tmp_path: Path) -> None:
    """无变更时不重写缓存文件（避免每次跑都动盘）。"""
    path = write(tmp_path / "m.py", DECL)
    cache = tmp_path / ".cache" / "idx.json"
    warm = ti.TableIndex(tmp_path, cache)
    warm.declarations(path)
    warm.save(prune=True)
    before = cache.stat().st_mtime_ns
    again = ti.TableIndex(tmp_path, cache)
    again.declarations(path)
    again.save(prune=True)
    assert cache.stat().st_mtime_ns == before


def test_checker_cache_matches_full_scan(tmp_path: Path) -> None:
    """Checker 层：开缓存与关缓存的违规集合完全一致（含表名冲突这类全局结论）。"""
    from test_base_boundaries import build, cfg_of, sample

    from engkit.base import service_boundaries as sb

    root = build(
        tmp_path,
        **{
            "pkg/services/alpha/src/alpha/services/dup.py": '__tablename__ = "sys_dup"\n',
            "pkg/services/beta/src/beta/services/dup.py": '__tablename__ = "sys_dup"\n',
        },
    )
    rule = sample(table_index_cache=".cache/boundaries/idx.json")
    cfg = cfg_of(root, rule)
    warm = sb.check(cfg, rule)
    cold = sb.check(cfg, rule, cache=False)
    assert set(warm.problems) == set(cold.problems)
    assert any("[表名跨服务重复]" in item for item in warm.problems)
    assert (root / ".cache" / "boundaries" / "idx.json").is_file()


def test_checker_cache_sees_new_table_conflict(tmp_path: Path) -> None:
    """缓存不是免检：新增同名表后重跑即报冲突（缓存按文件失效）。"""
    from test_base_boundaries import build, cfg_of, sample

    from engkit.base import service_boundaries as sb

    root = build(tmp_path)
    rule = sample(table_index_cache=".cache/boundaries/idx.json")
    cfg = cfg_of(root, rule)
    assert not any("[表名跨服务重复]" in item for item in sb.check(cfg, rule).problems)
    write(root / "pkg/services/alpha/src/alpha/services/new.py", '__tablename__ = "sys_new"\n')
    write(root / "pkg/services/beta/src/beta/services/new.py", '__tablename__ = "sys_new"\n')
    assert any("[表名跨服务重复]" in item for item in sb.check(cfg, rule).problems)


def test_checker_without_cache_config_writes_nothing(tmp_path: Path) -> None:
    """未配 `table_index_cache`（缺省空串）时 Checker 不落盘。"""
    from test_base_boundaries import build, cfg_of, sample

    from engkit.base import service_boundaries as sb

    root = build(tmp_path)
    sb.check(cfg_of(root, sample()), sample())
    assert not (root / ".cache").exists()


def test_is_model_module_tolerates_broken_file(tmp_path: Path) -> None:
    """`is_model_module` 遇到语法错文件返回 False（不让一个坏文件打断布局形态校验）。"""
    from test_base_boundaries import build, cfg_of, sample

    from engkit.base import service_boundaries as sb

    root = build(tmp_path, **{"pkg/broken.py": '__tablename__ = "x"\ndef f(:\n'})
    rule = sample()
    cfg = cfg_of(root, rule)
    checker = sb.Checker(cfg, rule, sb.load_registry(cfg, rule))
    assert checker.is_model_module("pkg/broken.py") is False


def test_index_ignores_malformed_rows(tmp_path: Path) -> None:
    """单条条目结构非法只跳过该条，不作废整份缓存（行号非 int / 表名非 str / 非二元组）。"""
    path = write(tmp_path / "m.py", DECL)
    cache = tmp_path / ".cache" / "idx.json"
    first = ti.TableIndex(tmp_path, cache)
    first.declarations(path)
    first.save(prune=True)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    entries = payload["entries"]["m.py"]
    entries[2] = ["not-a-row", [1, 2], [3], [5, "sys_a"]]
    cache.write_text(json.dumps(payload), encoding="utf-8")
    index = ti.TableIndex(tmp_path, cache)
    assert index.declarations(path) == [(5, "sys_a")]
    assert index.hits == 1
