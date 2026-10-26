"""测试夹具：在 `tmp_path` 造「迷你仓库」（含后端工程、前端包与文档根）。"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from engkit.config import Config, load

BASE_CONFIG = """
[repo]
name = "mini"

[docs]
root = "docs"
skip = ["docs/用户文档/"]

[docs.scope]
whitelist_dirs = ["规范/"]
whitelist_files = ["规划/总体项目规划.md"]
plan_dir_pattern = "^项目/[^/]+/计划/"

[backend]
root = "backend"
groups = ["libs", "services"]
projects = ["libs/pkg_a", "services/svc"]
coverage_packages = ["pkg_a", "svc"]

[frontend]
root = "frontend"
groups = ["packages", "apps"]
workspace_protocol = "workspace:"

[selection]
base_threshold = 100
base_ratio = 0.6

[rules.backend.global]
exact = ["backend/pyproject.toml"]
prefixes = ["backend/alembic/"]
config_prefix = "backend/config"
config_suffix = ".toml"

[[rules.backend.extra_tests]]
match_prefix = "backend/ops/"
test_dir_suffix = "/tests/ops/"
coverage = false

[[rules.backend.extra_tests]]
match_prefix = "scripts/tools/"
test_files = ["libs/pkg_a/tests/core/test_core.py"]

[[rules.backend.extra_tests]]
match_prefix = "scripts/check/"
fallback_dir = "libs/pkg_a/tests/boundary"
mapping = [{ script = "one.py", test = "libs/pkg_a/tests/core/test_core.py" }]

[rules.frontend.global]
exact = ["package.json"]
prefixes = ["scripts/"]

[coverage]
plugin = "plug"
pythonpath = ["backend"]

[pytest]
cmd = "python -m pytest"
cwd = "backend"
max_rounds = 2

[preflight]
steps = [
  { label = "静态：echo", cmd = ["{python}", "-c", "print('static')"] },
  { label = "快档专用", cmd = ["{python}", "-c", "print('fast')"], preset = "fast" },
  { label = "可选步骤", cmd = ["definitely-missing-binary"], optional = true },
]
self_tests = [
  { label = "自测：echo", cmd = ["{python}", "-c", "print('self')"], deps = ["scripts/tools/guard.py"] },
]

[runonce]
cache_dir = ".cache/run-once"
window_seconds = 1800

[guard]
scripts = ["check-preflight.py"]
repo_dirs = ["libs", "services"]
dir_test_threshold = 20

[kiwi]
env_file = "deploy/.env"
host_env = "MINI_HOST"
user_env = "MINI_USER"
container = "mini-kiwi"
"""

BASE_FILES: dict[str, str] = {
    "backend/libs/pkg_a/src/pkg_a/__init__.py": "",
    "backend/libs/pkg_a/src/pkg_a/core.py": "from pkg_a.util import helper\n\n\ndef run():\n    return helper()\n",
    "backend/libs/pkg_a/src/pkg_a/util.py": "def helper():\n    return 1\n",
    "backend/libs/pkg_a/tests/core/test_core.py": "from pkg_a.core import run\n\n\ndef test_run():\n    assert run()\n",
    "backend/libs/pkg_a/tests/test_root.py": "def test_root():\n    assert True\n",
    "backend/libs/pkg_a/tests/boundary/test_boundary.py": "def test_boundary():\n    assert True\n",
    "backend/services/svc/src/svc/__init__.py": "",
    "backend/services/svc/src/svc/app.py": "def ping():\n    return 'pong'\n",
    "backend/services/svc/tests/test_app.py": "from svc.app import ping\n\n\ndef test_ping():\n    assert ping()\n",
    "backend/ops/seed.py": "print('seed')\n",
    "docs/文档首页.md": "# 首页\n\n[规范](规范/规范.md)\n",
    "docs/规范/规范.md": '# 规范\n\n## 1. 节 <a id="sec"></a>\n',
    "docs/项目/X/计划/01_计划.md": "# 计划\n\n工时 8h\n",
    "docs/项目/X/任务/01_任务/01_任务.md": "# 任务\n\n交付完成。\n",
    "frontend/packages/core/package.json": '{"name": "@mini/core", "dependencies": {}}',
    "frontend/apps/desktop/package.json": '{"name": "@mini/desktop", "dependencies": {"@mini/core": "workspace:*"}}',
    "deploy/.env": "MINI_HOST=10.0.0.1\nMINI_USER=ops\n",
}


@pytest.fixture
def build_repo(tmp_path: Path):
    """返回「造迷你仓库」工厂。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        Callable[..., Path]: 造仓函数（可覆盖配置与文件）。
    """

    counter = {"n": 0}

    def build(config: str = BASE_CONFIG, files: dict[str, str] | None = None, name: str = "engkit.toml") -> Path:
        counter["n"] += 1
        root = tmp_path / f"repo{counter['n']}"
        merged = dict(BASE_FILES)
        merged.update(files or {})
        for rel, content in merged.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content), encoding="utf-8")
        (root / name).write_text(textwrap.dedent(config), encoding="utf-8")
        return root

    return build


@pytest.fixture
def repo(build_repo: Any) -> Path:
    """造好一个缺省迷你仓库。

    Args:
        build_repo: 造仓工厂。

    Returns:
        Path: 仓库根。
    """
    return build_repo()


@pytest.fixture
def cfg(repo: Path) -> Config:
    """迷你仓库的配置对象。

    Args:
        repo: 仓库根。

    Returns:
        Config: 配置。
    """
    return load(repo / "engkit.toml")


def git(root: Path, *args: str) -> None:
    """在仓库内执行 git 命令（测试用）。

    Args:
        root: 仓库根。
        *args: git 参数。
    """
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": __import__("os").environ.get("PATH", ""),
        },
    )


def init_git(root: Path) -> None:
    """初始化 git 仓库并提交一次。

    Args:
        root: 仓库根。
    """
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")


@pytest.fixture
def git_repo(repo: Path) -> Path:
    """把缺省迷你仓库变成已提交的 git 仓库。

    Args:
        repo: 仓库根。

    Returns:
        Path: 仓库根。
    """
    init_git(repo)
    return repo
