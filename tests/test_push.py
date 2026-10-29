"""推送入口用例（预检是推送的内部步骤）。"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from conftest import git

from engkit import push
from engkit.config import Step


def _remote(git_repo: Path, tmp_path: Path) -> Path:
    """造一个 bare 远端并把当前提交推上去（之后新增的提交才算「待推」）。

    Args:
        git_repo: 已提交的迷你仓库。
        tmp_path: pytest 临时目录。

    Returns:
        Path: bare 远端路径。
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(git_repo, "remote", "add", "origin", str(remote))
    git(git_repo, "push", "-q", "-u", "origin", "HEAD")
    return remote


def _commit(git_repo: Path, rel: str, content: str = "x\n") -> None:
    """写文件并提交。

    Args:
        git_repo: 仓库根。
        rel: 相对路径。
        content: 内容。
    """
    path = git_repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", f"add {rel}")


def test_is_doc() -> None:
    """按文件名模式判定文档（目录不参与匹配）。"""
    globs = ("*.md", "*.png")
    assert push.is_doc("docs/项目/01_任务.md", globs) is True
    assert push.is_doc("docs/资源/图.png", globs) is True
    assert push.is_doc("backend/app.py", globs) is False


def test_all_docs() -> None:
    """空集合不算「全为文档」。"""
    globs = ("*.md",)
    assert push.all_docs([], globs) is False
    assert push.all_docs(["a.md", "b.md"], globs) is True
    assert push.all_docs(["a.md", "b.py"], globs) is False


def test_resolve_mode() -> None:
    """全量优先；全文档走快速档；其余定向档。"""
    globs = ("*.md",)
    assert push.resolve_mode(["a.md"], globs, full=True) == "full"
    assert push.resolve_mode(["a.md"], globs, full=False) == "fast"
    assert push.resolve_mode(["a.py"], globs, full=False) == "scoped"


def test_push_detached_head(git_repo: Path, cfg, capsys) -> None:
    """游离 HEAD（无法确定推送分支）直接拒绝。"""
    git(git_repo, "checkout", "-q", "--detach", "HEAD")
    code = push.run_push(cfg, jobs="off")
    assert code == 1
    assert "游离 HEAD" in capsys.readouterr().out


def test_push_dirty(git_repo: Path, cfg, capsys) -> None:
    """工作树脏即拒绝（提示先提交）。"""
    (git_repo / "dirty.txt").write_text("x", encoding="utf-8")
    code = push.run_push(cfg, jobs="off")
    assert code == 1
    assert "未提交改动" in capsys.readouterr().out


def test_push_dirty_truncates(git_repo: Path, cfg, capsys) -> None:
    """脏条目超过 5 处时从略。"""
    for index in range(7):
        (git_repo / f"dirty{index}.txt").write_text("x", encoding="utf-8")
    assert push.run_push(cfg, jobs="off") == 1
    assert "其余 2 处从略" in capsys.readouterr().out


def test_push_nothing_to_push(git_repo: Path, cfg, tmp_path: Path, capsys) -> None:
    """没有待推提交时直接返回 0。"""
    _remote(git_repo, tmp_path)
    code = push.run_push(cfg, jobs="off")
    assert code == 0
    assert "没有待推送的提交" in capsys.readouterr().out


def test_push_docs_fast(git_repo: Path, cfg, tmp_path: Path, capsys) -> None:
    """待推提交全为文档：走快速档，`--check` 不推送。"""
    _remote(git_repo, tmp_path)
    _commit(git_repo, "docs/项目/记录.md")
    code = push.run_push(cfg, check=True, jobs="off")
    assert code == 0
    out = capsys.readouterr().out
    assert "档位：快速" in out
    assert "--check 未推送" in out


def test_push_code_scoped(git_repo: Path, cfg, tmp_path: Path, capsys) -> None:
    """含代码改动：走定向档。"""
    _remote(git_repo, tmp_path)
    _commit(git_repo, "backend/libs/pkg_a/src/pkg_a/new.py")
    assert push.run_push(cfg, check=True, jobs="off") == 0
    assert "档位：定向" in capsys.readouterr().out


def test_push_full(git_repo: Path, cfg, tmp_path: Path, capsys) -> None:
    """显式全量档优先于按变更选档。"""
    _remote(git_repo, tmp_path)
    _commit(git_repo, "docs/项目/记录.md")
    assert push.run_push(cfg, check=True, full=True, jobs="off") == 0
    assert "档位：全量" in capsys.readouterr().out


def test_push_remote_branch_missing(git_repo: Path, cfg, capsys) -> None:
    """远端无该分支：按代码改动处理并提示。"""
    _commit(git_repo, "docs/项目/记录.md")
    assert push.run_push(cfg, check=True, jobs="off") == 0
    assert "远端无该分支" in capsys.readouterr().out


def test_push_preflight_fails(git_repo: Path, cfg, tmp_path: Path, capsys) -> None:
    """预检未通过即中止推送。"""
    _remote(git_repo, tmp_path)
    _commit(git_repo, "docs/项目/记录.md")
    failing = dataclasses.replace(
        cfg,
        preflight=dataclasses.replace(
            cfg.preflight,
            steps=(Step(label="失败步骤", cmd=("{python}", "-c", "raise SystemExit(1)")),),
            self_tests=(),
        ),
    )
    assert push.run_push(failing, jobs="off") == 1
    assert "已中止推送" in capsys.readouterr().out


def test_push_success(git_repo: Path, cfg, tmp_path: Path, capsys) -> None:
    """预检通过后真推送成功。"""
    remote = _remote(git_repo, tmp_path)
    _commit(git_repo, "docs/项目/记录.md")
    assert push.run_push(cfg, jobs="off") == 0
    assert "推送完成" in capsys.readouterr().out
    heads = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert heads == remote_head


def test_push_failure_returns_code(git_repo: Path, cfg, capsys) -> None:
    """推送失败返回 git 的退出码（预检结论仍有效）。"""
    _commit(git_repo, "docs/项目/记录.md")
    git(git_repo, "remote", "add", "origin", str(Path(git_repo).parent / "missing.git"))
    assert push.run_push(cfg, jobs="off") != 0
    assert "推送失败" in capsys.readouterr().out


def test_push_explicit_remote_and_branch(git_repo: Path, cfg, tmp_path: Path) -> None:
    """显式指定远端与分支时按其执行。"""
    _remote(git_repo, tmp_path)
    _commit(git_repo, "docs/项目/记录.md")
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert push.run_push(cfg, check=True, remote="origin", branch=branch, jobs="off") == 0
