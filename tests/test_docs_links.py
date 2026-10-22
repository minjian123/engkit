"""文档链接与锚点校验用例。"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from engkit import docs_links
from engkit.config import load


def test_strip_code() -> None:
    """行内代码被等长空白替换（列位置不变）。"""
    text = "见 `[x](y.md)` 与 [a](b.md)"
    stripped = docs_links.strip_code(text)
    assert "[x](y.md)" not in stripped
    assert "[a](b.md)" in stripped
    assert len(stripped) == len(text)


def test_check_clean(cfg) -> None:
    """缺省夹具链接自洽。"""
    report = docs_links.check(cfg)
    assert report.broken == 0 and report.anchors == 0
    assert report.files == 4


def test_check_broken_and_anchor(build_repo) -> None:
    """断链与失效锚点均被报出。"""
    root = build_repo(
        files={
            "docs/坏链接.md": "# 坏\n\n[a](不存在.md)\n[b](规范/规范.md#没有这个锚点)\n[c](规范/规范.md#sec)\n",
        }
    )
    report = docs_links.check(load(root / "engkit.toml"))
    kinds = {(item.kind, item.target) for item in report.findings}
    assert ("断链", "不存在.md") in kinds
    assert ("失效锚点", "规范/规范.md#没有这个锚点") in kinds
    assert report.broken == 1 and report.anchors == 1


def test_check_percent_encoded_path(build_repo) -> None:
    """链接目标按 URL 编码解码后比较（目录名含空格写成 `%20` 不算断链）。"""
    root = build_repo(
        files={
            "docs/01_阶段 一/入口.md": "# 入口\n\n[a](../总览.md)\n",
            "docs/总览.md": "# 总览\n",
            "docs/引用.md": "# 引用\n\n[a](01_阶段%20一/入口.md)\n",
        }
    )
    report = docs_links.check(load(root / "engkit.toml"))
    assert report.broken == 0 and report.anchors == 0


def test_check_percent_encoded_anchor(build_repo) -> None:
    """锚点同样按 URL 编码解码（中文锚点编码后仍能命中）。"""
    root = build_repo(
        files={
            "docs/规范/规范.md": '# 规范\n\n<a id="定稿"></a>\n',
            "docs/引用.md": "# 引用\n\n[a](规范/规范.md#%E5%AE%9A%E7%A8%BF)\n",
        }
    )
    report = docs_links.check(load(root / "engkit.toml"))
    assert report.anchors == 0


def test_check_skips_external_and_inline(build_repo) -> None:
    """外部链接、行内代码与纯锚点不算问题。"""
    root = build_repo(
        files={
            "docs/正常.md": "# 正常\n\n[外](https://example.com)\n[邮件](mailto:a@b.c)\n"
            "[纯锚点](#top)\n`[内](不存在.md)`\n[空]()\n",
        }
    )
    report = docs_links.check(load(root / "engkit.toml"))
    assert report.findings == ()


def test_check_skips_local_only(build_repo) -> None:
    """`[docs].skip` 前缀内的文档与目标都不参与校验。"""
    root = build_repo(
        files={
            "docs/用户文档/本地.md": "# 本地\n\n[a](缺失.md)\n",
            "docs/引用本地.md": "# 引用\n\n[a](用户文档/本地.md)\n",
        }
    )
    report = docs_links.check(load(root / "engkit.toml"))
    assert report.findings == ()


def test_check_outside_root_link(build_repo) -> None:
    """跳出仓库根的链接按断链处理（不做跳过判定）。"""
    root = build_repo(files={"docs/出界.md": "# 出界\n\n[a](../../../外面.md)\n"})
    report = docs_links.check(load(root / "engkit.toml"))
    assert report.broken == 1


def test_check_missing_docs_root(cfg, tmp_path: Path) -> None:
    """文档根不存在时报错。"""
    broken = dataclasses.replace(cfg, docs=dataclasses.replace(cfg.docs, root="不存在"))
    with pytest.raises(FileNotFoundError):
        docs_links.check(broken)
