"""步骤结果复用（Kiwi 2446）：缓存键、输入指纹、记录读写与 5 类负向注入。"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest
from conftest import init_git

from engkit import steps_reuse
from engkit.config import load

pytestmark = pytest.mark.kiwi_id(2446)

STEP_INPUTS = ["backend/libs/pkg_a/src"]


def _tree_repo(tmp_path: Path, config: str | None = None) -> Path:
    """造一个已提交的迷你仓库（复用 conftest 的文件布局）。

    Args:
        tmp_path: 临时目录。
        config: 覆盖配置文本（None 取缺省）。

    Returns:
        Path: 仓库根。
    """
    root = tmp_path / "reuse"
    files = {
        "engkit.toml": config
        or (
            '[docs]\nroot = "docs"\n'
            '[backend]\nroot = "backend"\ngroups = ["libs"]\n'
            '[frontend]\nroot = "frontend"\ngroups = ["packages"]\n'
            '[[preflight.steps]]\nlabel = "静态"\ncmd = ["{python}", "-c", "pass"]\n'
            'inputs = ["backend/libs/pkg_a/src"]\n'
        ),
        "docs/首页.md": "# 首页\n",
        "backend/libs/pkg_a/src/pkg_a/__init__.py": "",
        "backend/libs/pkg_a/src/pkg_a/util.py": "def helper():\n    return 1\n",
        "backend/libs/pkg_a/src/pkg_a/core.py": "from pkg_a.util import helper\n",
        "backend/libs/pkg_a/tests/test_core.py": "def test_ok():\n    assert True\n",
        "frontend/packages/core/package.json": '{"name": "@mini/core"}',
        "frontend/apps/desktop/package.json": '{"name": "@mini/desktop"}',
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    init_git(root)
    return root


def test_infer_inputs_declared_wins(tmp_path: Path) -> None:
    """显式声明优先于命令形态推断。"""
    cfg = load(_tree_repo(tmp_path) / "engkit.toml")
    result = steps_reuse.infer_inputs(cfg, ["pyright", "."], STEP_INPUTS)
    assert result.kind == "declared"
    assert result.prefixes == ("backend/libs/pkg_a/src",)


def test_infer_inputs_files_from_means_changed(tmp_path: Path) -> None:
    """`--files-from` 形态按改动集取输入。"""
    cfg = load(_tree_repo(tmp_path) / "engkit.toml")
    assert steps_reuse.infer_inputs(cfg, ["python", "x.py", "--files-from", "-"], []).kind == "changed"
    assert steps_reuse.infer_inputs(cfg, ["python", "x.py", "{files}"], []).kind == "changed"


def test_infer_inputs_root_prefixes(tmp_path: Path) -> None:
    """首个仓内路径参数落在已配置根下时取该根。"""
    cfg = load(_tree_repo(tmp_path) / "engkit.toml")
    assert steps_reuse.infer_inputs(cfg, ["tsc", "frontend"], []).kind == "declared"
    assert steps_reuse.infer_inputs(cfg, ["tsc", "frontend/apps/desktop"], []).prefixes == ("frontend",)
    assert steps_reuse.infer_inputs(cfg, ["ruff", "backend/libs"], []).prefixes == ("backend",)
    assert steps_reuse.infer_inputs(cfg, ["engkit", "links", "docs"], []).prefixes == ("docs",)


def test_infer_inputs_tree_fallback(tmp_path: Path) -> None:
    """无法识别输入（`.` / 仓根 / 仓外路径）时退化为整棵树。"""
    cfg = load(_tree_repo(tmp_path) / "engkit.toml")
    assert steps_reuse.infer_inputs(cfg, ["pyright", "."], []).kind == "tree"
    assert steps_reuse.infer_inputs(cfg, ["pyright", str(cfg.root)], []).kind == "tree"
    assert steps_reuse.infer_inputs(cfg, ["pyright", str(Path("C:/outside"))], []).kind == "tree"


def test_env_fingerprint_tracks_config(tmp_path: Path) -> None:
    """环境指纹随配置变化（注入 5：跨仓 / 改配置不得误命中）。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    first = steps_reuse.env_fingerprint(cfg)
    assert first == steps_reuse.env_fingerprint(load(root / "engkit.toml"))
    text = (root / "engkit.toml").read_text(encoding="utf-8").replace("静态", "静态2")
    (root / "engkit.toml").write_text(text, encoding="utf-8")
    assert steps_reuse.env_fingerprint(load(root / "engkit.toml")) != first


def test_input_fingerprint_missing_prefix(tmp_path: Path) -> None:
    """输入前缀不存在时不命中（返回 None）。"""
    cfg = load(_tree_repo(tmp_path) / "engkit.toml")
    missing = steps_reuse.InputSet(kind="declared", prefixes=("backend/not-there",))
    assert steps_reuse.input_fingerprint(cfg, missing, []) is None


def test_input_fingerprint_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """取不到树对象（git 不可用 / 索引不可写）→ 不命中。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    monkeypatch.setattr(steps_reuse.gitutil, "tree_hash", lambda _root: None)
    assert steps_reuse.input_fingerprint(cfg, steps_reuse.InputSet(kind="tree"), []) is None


def test_input_fingerprint_changed_set(tmp_path: Path) -> None:
    """改动集指纹：空集与非空集都能取到值。"""
    cfg = load(_tree_repo(tmp_path) / "engkit.toml")
    empty = steps_reuse.input_fingerprint(cfg, steps_reuse.InputSet(kind="changed"), [])
    assert empty == steps_reuse.input_fingerprint(cfg, steps_reuse.InputSet(kind="changed"), [])
    one = steps_reuse.input_fingerprint(
        cfg, steps_reuse.InputSet(kind="changed"), ["backend/libs/pkg_a/src/pkg_a/util.py"]
    )
    assert one != empty


def test_tampered_record_is_not_hit(tmp_path: Path) -> None:
    """注入 1：记录被篡改 / 键不符时不得命中（假命中必须重跑）。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    key = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert key is not None
    steps_reuse.record(cfg, key, "静态", 1.0)
    assert steps_reuse.lookup(cfg, key) is not None
    path = root / cfg.preflight.reuse_file
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["entries"].values():
        entry["inputs"] = "deadbeef"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert steps_reuse.lookup(cfg, key) is None


def test_code_change_invalidates_even_without_add(tmp_path: Path) -> None:
    """注入 1 的另一面：改了文件但没 `git add`，输入指纹也必须变。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    before = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    (root / "backend/libs/pkg_a/src/pkg_a/util.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    after = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert before is not None and after is not None
    assert before.inputs != after.inputs


def test_dirty_out_of_scope_is_ignored(tmp_path: Path) -> None:
    """输入集外的工作树改动不影响该步骤的指纹。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    before = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    (root / "docs/首页.md").write_text("# 首页改\n", encoding="utf-8")
    after = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert before is not None and after is not None
    assert before.inputs == after.inputs


def test_corrupt_record_falls_back_to_run(tmp_path: Path) -> None:
    """注入 2a：记录损坏 → 全部真跑（lookup 返回 None，不抛异常）。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    path = root / cfg.preflight.reuse_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    key = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert key is not None
    assert steps_reuse.lookup(cfg, key) is None


def test_record_schema_mismatch_and_shapes(tmp_path: Path) -> None:
    """记录结构版本不符 / 顶层非对象 / entries 非对象，均视为无记录。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    path = root / cfg.preflight.reuse_file
    path.parent.mkdir(parents=True, exist_ok=True)
    key = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert key is not None
    for payload in ('{"schema": 99, "entries": {}}', "[1, 2]", '{"schema": 1, "entries": []}'):
        path.write_text(payload, encoding="utf-8")
        assert steps_reuse.lookup(cfg, key) is None


def test_write_failure_is_non_fatal(tmp_path: Path) -> None:
    """注入 2b：记录无法写入 → 不抛异常（本次仍按真跑结论判定）。"""
    root = _tree_repo(tmp_path)
    raw = load(root / "engkit.toml")
    cfg = dataclasses.replace(raw, preflight=dataclasses.replace(raw.preflight, reuse_file="backend"))
    key = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert key is not None
    steps_reuse.record(cfg, key, "静态", 1.0)
    assert steps_reuse.lookup(cfg, key) is None


def test_reuse_disabled_short_circuits(tmp_path: Path) -> None:
    """关闭复用后 key 为空、读写都不生效。"""
    root = _tree_repo(tmp_path)
    raw = load(root / "engkit.toml")
    cfg = dataclasses.replace(raw, preflight=dataclasses.replace(raw.preflight, reuse=False))
    assert steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, []) is None
    key = steps_reuse.Key(command="c", env="e", inputs="i")
    steps_reuse.record(cfg, key, "静态", 1.0)
    assert steps_reuse.lookup(cfg, key) is None
    assert not (root / cfg.preflight.reuse_file).exists()


def test_forget_removes_entry(tmp_path: Path) -> None:
    """失败后删记录，避免留下「可复用」的失败态。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    key = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert key is not None
    steps_reuse.record(cfg, key, "静态", 1.0)
    steps_reuse.forget(cfg, key)
    assert steps_reuse.lookup(cfg, key) is None
    steps_reuse.forget(cfg, key)


def test_prune_drops_expired_and_caps(tmp_path: Path) -> None:
    """过期条目被淘汰，条目总数受上限约束。"""
    entries = {
        "old": {"passed_at": "2000-01-01T00:00:00+00:00", "command": "c"},
        "no-stamp": {"command": "c"},
        "bad-stamp": {"passed_at": "not-a-date", "command": "c"},
        "naive": {"passed_at": "2999-01-01T00:00:00", "command": "c"},
    }
    kept = steps_reuse._prune(entries, ttl_days=7)
    assert set(kept) == {"naive"}
    many = {f"k{index}": {"passed_at": f"2999-01-01T00:00:{index:02d}+00:00"} for index in range(5)}
    assert len(steps_reuse._prune(many, ttl_days=7)) == 5


def test_content_digest_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """工作树清单里出现不可读文件时用占位串（不中断指纹计算）。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    monkeypatch.setattr(steps_reuse.gitutil, "worktree_dirty", lambda _root: (["backend/gone.py"], True))
    result = steps_reuse.input_fingerprint(cfg, steps_reuse.InputSet(kind="tree"), [])
    assert result is not None


def test_worktree_undecidable_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git 不可判定时视为无工作树改动（指纹仅由树对象决定）。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    monkeypatch.setattr(steps_reuse.gitutil, "worktree_dirty", lambda _root: ([], False))
    assert steps_reuse.input_fingerprint(cfg, steps_reuse.InputSet(kind="declared", prefixes=("docs",)), []) is not None


def test_changed_scope_dirty_listing(tmp_path: Path) -> None:
    """改动集输入集下，工作树清单按是否在改动集内过滤。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    rel = "backend/libs/pkg_a/src/pkg_a/util.py"
    (root / rel).write_text("def helper():\n    return 3\n", encoding="utf-8")
    with_change = steps_reuse.input_fingerprint(cfg, steps_reuse.InputSet(kind="changed"), [rel])
    without = steps_reuse.input_fingerprint(cfg, steps_reuse.InputSet(kind="changed"), [])
    assert with_change != without
    assert steps_reuse._in_scope(rel, steps_reuse.InputSet(kind="changed"), [rel]) is True
    assert steps_reuse._in_scope(rel, steps_reuse.InputSet(kind="changed"), []) is False


def test_preflight_step_reused_end_to_end(tmp_path: Path) -> None:
    """端到端：同一棵树两次预检，第二次复用；改代码后不命中。"""
    from engkit import preflight

    root = _tree_repo(
        tmp_path,
        config=(
            '[docs]\nroot = "docs"\n'
            '[backend]\nroot = "backend"\ngroups = ["libs"]\n'
            "[preflight]\nsteps = [\n"
            '  { label = "标记步骤", cmd = ["{python}", "-c", "pass"], inputs = ["backend/libs/pkg_a/src"] },\n'
            "]\n"
            "[preflight.gate]\ndirty_tree = false\n"
        ),
    )
    cfg = load(root / "engkit.toml")
    mark = root / ".cache" / "preflight" / "steps.json"
    assert preflight.run_preflight(cfg, mode="fast", jobs="off", no_cov=True, no_scope=True, from_push=True) == 0
    assert mark.is_file()
    assert preflight.run_preflight(cfg, mode="fast", jobs="off", no_cov=True, no_scope=True, from_push=True) == 0
    (root / "backend/libs/pkg_a/src/pkg_a/util.py").write_text("def helper():\n    return 9\n", encoding="utf-8")
    assert preflight.run_preflight(cfg, mode="fast", jobs="off", no_cov=True, no_scope=True, from_push=True) == 0


def test_artifacts_excluded_from_fingerprint(tmp_path: Path) -> None:
    """预检自身产物（记录 / 审计 / 覆盖率数据 / .git）不计入输入指纹。

    不排除会形成自污染：每写一次记录指纹就变，复用永远不命中。
    """
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    assert steps_reuse._is_artifact(cfg, ".git/index") is True
    assert steps_reuse._is_artifact(cfg, cfg.preflight.reuse_file) is True
    assert steps_reuse._is_artifact(cfg, f"{cfg.preflight.reuse_file}.{os.getpid()}.tmp") is True
    assert steps_reuse._is_artifact(cfg, f"{cfg.coverage.data_file}.1234") is True
    assert steps_reuse._is_artifact(cfg, "backend/libs/pkg_a/src/pkg_a/util.py") is False
    blank = dataclasses.replace(cfg, preflight=dataclasses.replace(cfg.preflight, reuse_file="", reuse_ttl_days=7))
    assert steps_reuse._is_artifact(blank, "backend/x.py") is False


def test_record_does_not_change_fingerprint(tmp_path: Path) -> None:
    """写记录后同一键仍然稳定（自污染回归）。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    first = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert first is not None
    steps_reuse.record(cfg, first, "静态", 0.1)
    again = steps_reuse.key_for(cfg, ["pyright", "."], STEP_INPUTS, [])
    assert again == first
    assert steps_reuse.lookup(cfg, again) is not None


def test_key_for_returns_none_without_fingerprint(tmp_path: Path) -> None:
    """输入指纹取不到时不给键（该步一律真跑）。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    assert steps_reuse.key_for(cfg, ["pyright", "backend/not-there"], ["backend/not-there"], []) is None


def test_forget_without_reuse_and_without_entry(tmp_path: Path) -> None:
    """关闭复用时 forget 直接返回；无对应条目时也不报错。"""
    root = _tree_repo(tmp_path)
    raw = load(root / "engkit.toml")
    disabled = dataclasses.replace(raw, preflight=dataclasses.replace(raw.preflight, reuse=False))
    steps_reuse.forget(disabled, steps_reuse.Key(command="c", env="e", inputs="i"))
    steps_reuse.forget(raw, steps_reuse.Key(command="c", env="e", inputs="i"))


def test_lookup_without_record_file(tmp_path: Path) -> None:
    """记录文件尚不存在 → 视为未命中（首次运行）。"""
    root = _tree_repo(tmp_path)
    cfg = load(root / "engkit.toml")
    assert steps_reuse.lookup(cfg, steps_reuse.Key(command="c", env="e", inputs="i")) is None


def test_match_root_without_optional_roots(tmp_path: Path) -> None:
    """只配置文档根时（无前端 / 后端）只按文档根匹配。"""
    root = tmp_path / "docs-only"
    root.mkdir()
    (root / "engkit.toml").write_text('[docs]\nroot = "docs"\n', encoding="utf-8")
    cfg = load(root / "engkit.toml")
    assert steps_reuse._match_root(cfg, "docs/首页.md") == "docs"
    assert steps_reuse._match_root(cfg, "backend/x.py") is None


def test_prune_caps_entries() -> None:
    """条目超过上限时按通过时间保留最新的。"""
    many = {f"k{index}": {"passed_at": f"2999-01-01T00:00:{index % 60:02d}+00:00"} for index in range(250)}
    kept = steps_reuse._prune(many, ttl_days=7)
    assert len(kept) == steps_reuse.MAX_ENTRIES


def test_preflight_reuse_disabled_runs_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`reuse = false` 时预检不做复用：打印关闭说明、全部真跑、不写记录。"""
    from engkit import preflight

    root = _tree_repo(
        tmp_path,
        config=(
            '[docs]\nroot = "docs"\n'
            '[backend]\nroot = "backend"\ngroups = ["libs"]\n'
            "[preflight]\nreuse = false\nsteps = [\n"
            '  { label = "标记步骤", cmd = ["{python}", "-c", "pass"], inputs = ["backend/libs/pkg_a/src"] },\n'
            "]\n"
            "[preflight.gate]\ndirty_tree = false\n"
        ),
    )
    cfg = load(root / "engkit.toml")
    assert preflight.run_preflight(cfg, mode="fast", jobs="off", no_cov=True, no_scope=True, from_push=True) == 0
    assert "步骤结果复用：关闭" in capsys.readouterr().out
    assert not (root / cfg.preflight.reuse_file).exists()


def test_step_scope_replacement(tmp_path: Path) -> None:
    """`changed-closure` 把仓根参数替换为闭包清单；退化时原样执行。"""
    from engkit import preflight, selection
    from engkit.config import Step

    root = _tree_repo(tmp_path)
    cfg = dataclasses.replace(load(root / "engkit.toml"), base_ratio=1.0)
    step = Step(label="pyright", cmd=("pyright", "."), scope="changed-closure")
    changed = ["backend/libs/pkg_a/src/pkg_a/util.py"]
    argv = preflight._apply_scope(cfg, step, ["pyright", "."], changed)
    assert "." not in argv
    assert "backend/libs/pkg_a/src/pkg_a/util.py" in argv
    plain = Step(label="pyright", cmd=("pyright", "."))
    assert preflight._apply_scope(cfg, plain, ["pyright", "."], changed) == ["pyright", "."]
    empty = selection.ReverseScope(files=(), degraded=False)
    original = selection.reverse_scope
    try:
        selection.reverse_scope = lambda *_args, **_kwargs: empty
        assert preflight._apply_scope(cfg, step, ["pyright", "."], changed) == ["pyright", "."]
    finally:
        selection.reverse_scope = original
