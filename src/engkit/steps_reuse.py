"""步骤结果复用：缓存键、输入集指纹、记录读写（唯一实现）。

判据（红线，见《预检耗时治理 · 详细设计》§3）：

1. **只有「同输入且该输入下已通过」才跳过**——输入变、命令变、工具链变、键缺失、记录损坏，一律真跑。
2. **fail-open 到执行**：任何异常路径的默认动作是「跑」，异常只打印原因，不改变判定。
3. 键由三部分组成、缺一不可：步骤命令全文 + 环境指纹 + 输入指纹。
4. 输入集只认「目录前缀 / 单文件」，靠 git 树对象取哈希（O(1)、不读盘）；取不到即不命中。

本模块不导入 `preflight`（避免环）：调用方负责展开占位符后传入 argv。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import gitutil
from .config import Config
from .console import note
from .version import VERSION

SCHEMA = 1
"""记录结构版本（结构变更即整体失效）。"""

MAX_ENTRIES = 200
"""记录条目上限（超出按通过时间淘汰最旧）。"""

_LOADED: dict[str, tuple[tuple[int, int], dict[str, dict[str, Any]]]] = {}
"""进程内记录缓存：路径 → （(mtime_ns, size), 条目）；按文件指纹校验外部更新。"""


@dataclass(frozen=True)
class InputSet:
    """步骤的输入集。

    Attributes:
        kind: `tree`（整棵树）/ `changed`（当次改动集）/ `declared`（显式前缀）。
        prefixes: 显式前缀（仓根相对；仅 `declared` 有效）。
    """

    kind: str
    prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Key:
    """缓存键。

    Attributes:
        command: 步骤命令全文（已展开占位符，空格拼接）。
        env: 环境指纹。
        inputs: 输入指纹。
    """

    command: str
    env: str
    inputs: str


@dataclass(frozen=True)
class Hit:
    """命中的历史记录。

    Attributes:
        passed_at: 上次通过的 ISO 时间。
        duration: 上次耗时（秒）。
    """

    passed_at: str
    duration: float


def env_fingerprint(cfg: Config) -> str:
    """计算环境指纹（包版本 + 解释器版本 + 依赖锁 + 仓配置）。

    Args:
        cfg: 仓配置。

    Returns:
        str: 指纹十六进制摘要。
    """
    digest = hashlib.sha256()
    digest.update(VERSION.encode())
    digest.update(sys.version.encode())
    for rel in ("pyproject.toml", "uv.lock"):
        digest.update(_file_digest(cfg.root / rel).encode())
    digest.update(_file_digest(cfg.path).encode())
    return digest.hexdigest()


def infer_inputs(cfg: Config, argv: Sequence[str], declared: Sequence[str]) -> InputSet:
    """推断步骤的输入集（声明优先，其次按命令形态，最后退化为整树）。

    Args:
        cfg: 仓配置。
        argv: 已展开占位符的命令。
        declared: 仓配置中的显式前缀。

    Returns:
        InputSet: 输入集。
    """
    if declared:
        return InputSet(kind="declared", prefixes=tuple(sorted(set(declared))))
    if "--files-from" in argv or "{files}" in argv:
        return InputSet(kind="changed")
    for token in argv:
        rel = _relative(cfg.root, token)
        if rel is None:
            continue
        if rel in ("", "."):
            break
        matched = _match_root(cfg, rel)
        if matched:
            return InputSet(kind="declared", prefixes=(matched,))
    return InputSet(kind="tree")


def input_fingerprint(cfg: Config, input_set: InputSet, changed: Sequence[str]) -> str | None:
    """计算输入指纹（索引树对象 + 工作树未提交内容的摘要）。

    只取树对象会漏掉「改了但没 add」的文件，而预检刚好处在提交前后两种状态；故同时把
    工作树相对索引的差异（含未跟踪文件）按输入集过滤后计入，宁可多失效也不误命中。

    Args:
        cfg: 仓配置。
        input_set: 输入集。
        changed: 当次改动文件（仓根相对；`changed` 输入集用）。

    Returns:
        str | None: 指纹；输入不可解析时返回 None（调用方据此不命中）。
    """
    tree = gitutil.tree_hash(cfg.root)
    if tree is None:
        return None
    if input_set.kind == "tree":
        digest = hashlib.sha256(tree.encode())
        for rel, value in _dirty_digests(cfg, input_set, changed):
            digest.update(f"{rel}:{value}\n".encode())
        return digest.hexdigest()
    digest = hashlib.sha256()
    if input_set.kind == "changed":
        items = sorted({gitutil.norm(item) for item in changed if item})
        if not items:
            return hashlib.sha256(b"changed:empty").hexdigest()
        for rel in items:
            digest.update(f"{rel}:{gitutil.object_hash(cfg.root, tree, rel) or '-'}\n".encode())
        return digest.hexdigest()
    for prefix in input_set.prefixes:
        value = gitutil.object_hash(cfg.root, tree, prefix.strip("/"))
        if value is None:
            note(f"复用未命中（输入前缀不存在：{prefix}）")
            return None
        digest.update(f"{prefix}:{value}\n".encode())
    for rel, value in _dirty_digests(cfg, input_set, changed):
        digest.update(f"{rel}:{value}\n".encode())
    return digest.hexdigest()


def _dirty_digests(cfg: Config, input_set: InputSet, changed: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """取落在输入集内的工作树未提交文件及其内容摘要。

    Args:
        cfg: 仓配置。
        input_set: 输入集。
        changed: 当次改动文件。

    Returns:
        tuple[tuple[str, str], ...]: （相对路径, 内容摘要）按路径排序。
    """
    entries, ok = gitutil.worktree_dirty(cfg.root)
    if not ok:
        return ()
    found: list[tuple[str, str]] = []
    for rel in sorted(gitutil.norm(item) for item in entries):
        if _is_artifact(cfg, rel) or not _in_scope(rel, input_set, changed):
            continue
        found.append((rel, _content_digest(cfg.root / rel)))
    return tuple(found)


def _is_artifact(cfg: Config, rel: str) -> bool:
    """判断路径是否属于「预检自身产物」（不参与输入指纹）。

    复用一个必须显式回避的坑：记录文件本身写在仓内（`.cache/preflight/steps.json`），若把它计入
    输入指纹，则「每写一次记录、指纹就变一次」，复用永远不命中。审计日志与覆盖率采集数据同理
    （每轮都在追加 / 按进程号落盘）。不能只依赖各仓 `.gitignore` 已忽略 `.cache/`——那是隐含前提。

    Args:
        cfg: 仓配置。
        rel: 仓根相对路径。

    Returns:
        bool: 属于自身产物返回 True（不计入指纹）。
    """
    if rel == ".git" or rel.startswith(".git/"):
        return True
    for name in (cfg.preflight.reuse_file, cfg.preflight.gate.audit_file, cfg.coverage.data_file):
        candidate = name.strip("/")
        if not candidate:
            continue
        if rel == candidate or rel.startswith(f"{candidate}."):
            return True
    return False


def _in_scope(rel: str, input_set: InputSet, changed: Sequence[str]) -> bool:
    """判断某个工作树改动是否落在输入集内。

    Args:
        rel: 仓根相对路径。
        input_set: 输入集。
        changed: 当次改动文件。

    Returns:
        bool: 落在输入集内返回 True。
    """
    if input_set.kind == "tree":
        return True
    if input_set.kind == "changed":
        return rel in {gitutil.norm(item) for item in changed}
    return any(rel == prefix.rstrip("/") or rel.startswith(f"{prefix.rstrip('/')}/") for prefix in input_set.prefixes)


def _content_digest(path: Path) -> str:
    """文件内容摘要（不可读时返回占位串）。

    Args:
        path: 文件路径。

    Returns:
        str: 十六进制摘要或占位串。
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "-"


def key_for(cfg: Config, argv: Sequence[str], declared: Sequence[str], changed: Sequence[str]) -> Key | None:
    """构造缓存键。

    Args:
        cfg: 仓配置。
        argv: 已展开占位符的命令。
        declared: 仓配置中的显式输入前缀。
        changed: 当次改动文件。

    Returns:
        Key | None: 缓存键；输入不可解析或复用关闭时返回 None。
    """
    if not cfg.preflight.reuse:
        return None
    inputs = input_fingerprint(cfg, infer_inputs(cfg, argv, declared), changed)
    if inputs is None:
        return None
    return Key(command=" ".join(argv), env=env_fingerprint(cfg), inputs=inputs)


def lookup(cfg: Config, key: Key) -> Hit | None:
    """查历史记录（键完全相等且该键下已通过才算命中）。

    Args:
        cfg: 仓配置。
        key: 缓存键。

    Returns:
        Hit | None: 命中返回历史记录；否则 None。
    """
    if not cfg.preflight.reuse:
        return None
    entry = _load(cfg).get(_key_id(key))
    if entry is None:
        return None
    if entry.get("command") != key.command or entry.get("env") != key.env or entry.get("inputs") != key.inputs:
        return None
    passed_at = entry.get("passed_at")
    duration = entry.get("duration")
    return Hit(
        passed_at=str(passed_at) if passed_at else "未知",
        duration=float(duration) if isinstance(duration, int | float) else 0.0,
    )


def record(cfg: Config, key: Key, label: str, duration: float) -> None:
    """记录一次通过（写失败只打印原因，不影响本次判定）。

    Args:
        cfg: 仓配置。
        key: 缓存键。
        label: 步骤标签。
        duration: 本次耗时（秒）。
    """
    if not cfg.preflight.reuse:
        return
    entries = dict(_load(cfg))
    entries[_key_id(key)] = {
        "command": key.command,
        "env": key.env,
        "inputs": key.inputs,
        "passed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration": round(duration, 3),
        "label": label,
    }
    _save(cfg, _prune(entries, cfg.preflight.reuse_ttl_days))


def forget(cfg: Config, key: Key) -> None:
    """删除某键的记录（失败步骤不得留「可复用」记录）。

    Args:
        cfg: 仓配置。
        key: 缓存键。
    """
    if not cfg.preflight.reuse:
        return
    entries = dict(_load(cfg))
    if entries.pop(_key_id(key), None) is None:
        return
    _save(cfg, entries)


def _key_id(key: Key) -> str:
    """把键压成记录用的标识。

    Args:
        key: 缓存键。

    Returns:
        str: 十六进制摘要。
    """
    digest = hashlib.sha256()
    for part in (key.command, key.env, key.inputs):
        digest.update(part.encode())
        digest.update(b"\x1f")
    return digest.hexdigest()


def _path(cfg: Config) -> Path:
    """记录文件路径。

    Args:
        cfg: 仓配置。

    Returns:
        Path: 绝对路径。
    """
    return cfg.root / cfg.preflight.reuse_file


def _file_digest(path: Path) -> str:
    """文件内容摘要（不存在时返回占位串）。

    Args:
        path: 文件路径。

    Returns:
        str: 十六进制摘要或占位串。
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "-"


def _relative(root: Path, token: str) -> str | None:
    """把命令参数换算成仓根相对路径。

    Args:
        root: 仓库根。
        token: 命令参数。

    Returns:
        str | None: 仓根相对路径；不是仓内路径时返回 None。
    """
    if not token or token.startswith("-"):
        return None
    candidate = Path(token)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return None
        return rel.as_posix() or "."
    return gitutil.norm(token).strip("/") or "."


def _match_root(cfg: Config, rel: str) -> str | None:
    """判断路径是否落在某个已配置根下（取最长匹配，避免嵌套误判）。

    Args:
        cfg: 仓配置。
        rel: 仓根相对路径。

    Returns:
        str | None: 命中的根（仓根相对）；未命中返回 None。
    """
    roots: list[str] = [cfg.docs.root]
    if cfg.frontend is not None:
        roots.append(cfg.frontend.root)
    if cfg.backend is not None:
        roots.append(cfg.backend.root)
    for root in sorted({item.strip("/") for item in roots if item}, key=len, reverse=True):
        if rel == root or rel.startswith(f"{root}/"):
            return root
    return None


def _parse_stamp(value: object) -> datetime | None:
    """解析记录时间戳。

    Args:
        value: 记录中的 `passed_at`。

    Returns:
        datetime | None: 解析结果；无法解析返回 None。
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _prune(entries: dict[str, dict[str, Any]], ttl_days: int) -> dict[str, dict[str, Any]]:
    """淘汰过期条目并限制总量。

    Args:
        entries: 条目表。
        ttl_days: 过期天数。

    Returns:
        dict[str, dict[str, Any]]: 保留的条目。
    """
    limit = datetime.now(UTC) - timedelta(days=max(ttl_days, 0))
    kept: dict[str, dict[str, Any]] = {}
    for key, entry in entries.items():
        stamp = _parse_stamp(entry.get("passed_at"))
        if stamp is None or stamp < limit:
            continue
        kept[key] = entry
    if len(kept) <= MAX_ENTRIES:
        return kept
    ordered = sorted(kept.items(), key=lambda item: str(item[1].get("passed_at", "")), reverse=True)
    return dict(ordered[:MAX_ENTRIES])


def _read(path: Path) -> dict[str, dict[str, Any]]:
    """读记录文件（任何异常都视为「无记录」，打印一次原因）。

    Args:
        path: 记录文件路径。

    Returns:
        dict[str, dict[str, Any]]: 条目表。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:  # pragma: no cover - `_load` 已先行 stat，仅在竞态删除时可达
        return {}
    except (OSError, ValueError) as error:
        note(f"复用不可用（记录损坏：{error}）；本次全部真跑")
        return {}
    if not isinstance(raw, dict):
        return {}
    payload = dict(raw)
    if payload.get("schema") != SCHEMA:
        note("复用不可用（记录结构版本不符）；本次全部真跑")
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {str(key): dict(value) for key, value in entries.items() if isinstance(value, dict)}


def _load(cfg: Config) -> dict[str, dict[str, Any]]:
    """取记录条目（按文件指纹使用进程内缓存）。

    Args:
        cfg: 仓配置。

    Returns:
        dict[str, dict[str, Any]]: 条目表（调用方不得原地修改）。
    """
    path = _path(cfg)
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _LOADED.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    entries = _read(path)
    _LOADED[str(path)] = (stamp, entries)
    return entries


def _save(cfg: Config, entries: dict[str, dict[str, Any]]) -> None:
    """原子写记录文件（写失败只打印原因）。

    Args:
        cfg: 仓配置。
        entries: 条目表。
    """
    path = _path(cfg)
    payload = json.dumps({"schema": SCHEMA, "entries": entries}, ensure_ascii=False, indent=2)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    except OSError as error:
        note(f"复用记录写入失败（{error}）；本次仍按真跑结论判定")
        return
    _LOADED.pop(str(path), None)
