"""重命令回放用例。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from engkit import runonce


def _run(root: Path, cache_dir: Path, *args: str, **kwargs) -> int:
    """执行一次包装。

    Args:
        root: 仓库根。
        cache_dir: 缓存目录。
        *args: 命令参数。
        **kwargs: 透传参数。

    Returns:
        int: 退出码。
    """
    return runonce.run(root, [sys.executable, *args], cache_dir=cache_dir, **kwargs)


def test_cache_key_stable() -> None:
    """缓存键对命令 / 目录 / 指纹敏感。"""
    first = runonce.cache_key(["a"], "/tmp", "fp")
    assert first == runonce.cache_key(["a"], "/tmp", "fp")
    assert first != runonce.cache_key(["a"], "/tmp", "other")


def test_save_and_load(tmp_path: Path) -> None:
    """记录写读往返。"""
    cache = tmp_path / "cache"
    record = runonce.Record(key="k1", argv=("a",), stdout="out", exit_code=3)
    runonce.save(cache, record)
    loaded = runonce.load(cache, "k1")
    assert loaded is not None
    assert loaded.exit_code == 3
    assert loaded.argv == ("a",)


def test_load_missing_and_corrupt(tmp_path: Path) -> None:
    """缺失 / 损坏 / 字段不符的记录按无缓存处理。"""
    cache = tmp_path / "cache"
    assert runonce.load(cache, "none") is None
    cache.mkdir()
    path = runonce.cache_path(cache, "bad")
    path.write_text("not json", encoding="utf-8")
    assert runonce.load(cache, "bad") is None
    path.write_text(json.dumps({"unknown": 1}), encoding="utf-8")
    assert runonce.load(cache, "bad") is None


def test_clip(monkeypatch) -> None:
    """输出截断。"""
    assert runonce.clip("short") == ("short", False)
    monkeypatch.setattr(runonce, "_MAX_OUTPUT_CHARS", 3)
    text, truncated = runonce.clip("abcdef")
    assert truncated is True
    assert text.startswith("abc")


def test_emit(capsys) -> None:
    """空串不输出；无换行时补换行。"""
    runonce.emit("")
    assert capsys.readouterr().out == ""
    runonce.emit("a")
    runonce.emit("b\n")
    assert capsys.readouterr().out == "a\nb\n"


def test_run_and_replay(git_repo: Path, tmp_path: Path, capsys) -> None:
    """首次真跑并在窗口内回放。"""
    cache = tmp_path / "cache"
    assert _run(git_repo, cache, "-c", "print('hello')", window_seconds=600) == 0
    first = capsys.readouterr().out
    assert "hello" in first
    assert "已记录" in first
    assert _run(git_repo, cache, "-c", "print('hello')", window_seconds=600) == 0
    second = capsys.readouterr().out
    assert "回放上次结果" in second
    assert "hello" in second


def test_run_force(git_repo: Path, tmp_path: Path, capsys) -> None:
    """`--force` 忽略缓存真跑。"""
    cache = tmp_path / "cache"
    _run(git_repo, cache, "-c", "print('x')", window_seconds=600)
    capsys.readouterr()
    _run(git_repo, cache, "-c", "print('x')", window_seconds=600, force=True)
    assert "--force 真跑" in capsys.readouterr().out


def test_run_expired_window(git_repo: Path, tmp_path: Path, capsys) -> None:
    """窗口已过即真跑。"""
    cache = tmp_path / "cache"
    _run(git_repo, cache, "-c", "print('x')", window_seconds=0)
    capsys.readouterr()
    _run(git_repo, cache, "-c", "print('x')", window_seconds=0)
    assert "缓存已过期" in capsys.readouterr().out


def test_run_show(git_repo: Path, tmp_path: Path, capsys) -> None:
    """`--show` 只报告缓存状态。"""
    cache = tmp_path / "cache"
    _run(git_repo, cache, "-c", "print('x')", window_seconds=600, show_only=True)
    assert "无缓存" in capsys.readouterr().out
    _run(git_repo, cache, "-c", "print('x')", window_seconds=600)
    capsys.readouterr()
    _run(git_repo, cache, "-c", "print('x')", window_seconds=600, show_only=True)
    assert "缓存命中" in capsys.readouterr().out


def test_run_exit_code(git_repo: Path, tmp_path: Path) -> None:
    """退出码透传并记录。"""
    cache = tmp_path / "cache"
    assert _run(git_repo, cache, "-c", "raise SystemExit(3)", window_seconds=600) == 3
    assert _run(git_repo, cache, "-c", "raise SystemExit(3)", window_seconds=600) == 3


def test_run_truncated_replay(git_repo: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    """截断记录在回放时提示。"""
    monkeypatch.setattr(runonce, "_MAX_OUTPUT_CHARS", 1)
    cache = tmp_path / "cache"
    _run(git_repo, cache, "-c", "print('abcdef')", window_seconds=600)
    capsys.readouterr()
    _run(git_repo, cache, "-c", "print('abcdef')", window_seconds=600)
    assert "上次输出曾截断" in capsys.readouterr().out
