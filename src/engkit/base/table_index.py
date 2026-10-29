"""表声明索引：`文件 → (行号, 表名)`，带跨进程持久化缓存。

为什么需要：表名 / 表前缀的**跨服务唯一性**无法局部判定，只能看全仓所有 `__tablename__` 声明；
这条全局判据在增量模式下也必须全量跑（裁剪即结论失效）。它的成本随文件数线性增长——
全量跑一次要读每个源文件的文本、再对命中标记的文件做 AST 解析。

缓存口径（与 mypy 同路的「元数据即键」）：

- 键为 `(mtime_ns, size)`：文件没变就连读都不读，直接命中；
- 只缓存**解析结果**（行号 + 表名），不缓存任何跨文件结论——跨文件判定每次现算；
- `version` 常量随解析口径变更递增，旧缓存整体作废；
- 缓存目录由仓侧配置声明（`[rules.service_boundaries].table_index_cache`），**默认关闭**：
  包内不假设任何仓的忽略规则，没配就不写盘；
- 写入走临时文件 + 原子替换，读失败 / 写失败都**降级放行**（当作没有缓存，只影响速度不影响结论）；
- 全量模式收尾时按「本轮所见」剪枝，删除文件的条目随之清掉；增量模式不剪枝（没见全，剪了会误删）。

**残余风险**：时间戳粒度粗于一次改写的文件系统（个别网络盘为 1 秒）上，同秒内改写且大小不变
会命中旧条目。故推送前预检的该步骤显式带 `--no-cache`，权威门禁永远现扫。
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .rules import read_text

__all__ = ["CACHE_VERSION", "TableIndex", "declared_tables"]

CACHE_VERSION = 1
"""缓存格式版本（解析口径变更时递增，旧缓存整体作废）。"""

TABLENAME_MARKER = "__tablename__"
"""表声明标记：源码不含该字面即不可能有 `__tablename__` 赋值，跳过 AST 解析。

表声明只能来自裸标识符赋值（`Name(id="__tablename__")`），而 `Name` 必由该字面解析而来；
故「先看字面、再解析」与「直接解析」等价，纯属省掉绝大多数文件的 AST 开销。
"""

Declaration = tuple[int, str]


def declared_tables(path: Path) -> list[Declaration]:
    """取文件内 `__tablename__` 表声明（表名小写）。

    不含表声明标记的文件直接返回空表、不做 AST 解析（见 `TABLENAME_MARKER`）。

    Args:
        path: 源文件路径。

    Returns:
        list[Declaration]: 行号与表名。
    """
    text = read_text(path)
    if TABLENAME_MARKER not in text:
        return []
    tree = ast.parse(text)
    found: list[Declaration] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == TABLENAME_MARKER for target in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            found.append((node.lineno, node.value.value.lower()))
    return found


def _rows_of(raw: Sequence[object]) -> list[Declaration]:
    """把缓存里的裸列表收窄成声明元组（结构不合法的单条忽略，不整份作废）。

    Args:
        raw: 缓存中的 `[[行号, 表名], ...]`。

    Returns:
        list[Declaration]: 声明清单。
    """
    rows: list[Declaration] = []
    for row in raw:
        if not isinstance(row, list) or len(row) != 2:
            continue
        lineno, table = row
        if isinstance(lineno, int) and isinstance(table, str):
            rows.append((lineno, table))
    return rows


class TableIndex:
    """表声明索引（按文件元数据命中缓存，未命中才解析）。"""

    def __init__(self, root: Path, cache_file: Path | None) -> None:
        """初始化。

        Args:
            root: 仓库根（相对路径基准）。
            cache_file: 缓存文件路径；None 表示不读不写（纯内存索引）。
        """
        self._root = root
        self._cache_file = cache_file
        self._entries: dict[str, tuple[int, int, list[list[object]]]] = {}
        self._seen: set[str] = set()
        self._dirty = False
        self._loaded = False
        self.hits = 0
        """命中缓存的文件数（度量 / 测试用）。"""
        self.misses = 0
        """需要解析的文件数（度量 / 测试用）。"""

    # ---- 缓存文件 ----

    def _load(self) -> None:
        """载入缓存（不存在 / 不可读 / 格式非法 / 版本不符都当作空缓存）。"""
        if self._loaded:
            return
        self._loaded = True
        if self._cache_file is None or not self._cache_file.is_file():
            return
        try:
            data = json.loads(read_text(self._cache_file))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return
        raw = data.get("entries")
        if not isinstance(raw, dict):
            return
        for rel, item in raw.items():
            if not isinstance(item, list) or len(item) != 3:
                continue
            mtime, size, rows = item
            if not isinstance(mtime, int) or not isinstance(size, int) or not isinstance(rows, list):
                continue
            self._entries[str(rel)] = (mtime, size, rows)

    def _rel(self, path: Path) -> str:
        """取相对仓根的 posix 路径。

        Args:
            path: 源文件路径。

        Returns:
            str: 相对路径。
        """
        try:
            return path.relative_to(self._root).as_posix()
        except ValueError:
            return path.as_posix()

    # ---- 查询 ----

    def declarations(self, path: Path) -> list[Declaration]:
        """取文件内表声明（命中缓存不读文件）。

        Args:
            path: 源文件路径。

        Returns:
            list[Declaration]: 行号与表名。
        """
        self._load()
        rel = self._rel(path)
        try:
            stat = path.stat()
        except OSError:
            # 枚举与读取之间被删：按「无声明」处理（下一次全量跑会随剪枝清掉条目）。
            self.misses += 1
            self._seen.add(rel)
            return []
        cached = self._entries.get(rel)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            self.hits += 1
            self._seen.add(rel)
            return _rows_of(cached[2])
        self.misses += 1
        found = declared_tables(path)
        self._entries[rel] = (stat.st_mtime_ns, stat.st_size, [[lineno, table] for lineno, table in found])
        self._seen.add(rel)
        self._dirty = True
        return found

    # ---- 落盘 ----

    def save(self, *, prune: bool) -> None:
        """写回缓存（不可写即静默跳过：缓存只影响速度，不影响结论）。

        Args:
            prune: 是否剪掉本轮未见的条目（仅全量模式可传 True——那才见全）。
        """
        if self._cache_file is None:
            return
        if prune:
            for rel in [rel for rel in self._entries if rel not in self._seen]:
                del self._entries[rel]
                self._dirty = True
        if not self._dirty:
            return
        payload = {"version": CACHE_VERSION, "entries": self._entries}
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            handle, temp = tempfile.mkstemp(dir=self._cache_file.parent, suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp, self._cache_file)
        except OSError:
            return
        self._dirty = False
