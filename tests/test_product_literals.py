"""产品字面量护栏用例（`[rules.product_literals]` + `engkit base product-literals`）。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from engkit import gitutil
from engkit.base import product_literals as pl
from engkit.config import ConfigError, parse


def _cfg(root: Path, rules: dict | None = None) -> object:
    """造一份最小配置。

    Args:
        root: 仓库根。
        rules: `[rules.product_literals]` 段（None 表示不登记）。

    Returns:
        object: 配置对象。
    """
    raw: dict = {"docs": {"root": "docs"}}
    if rules is not None:
        raw["rules"] = {"product_literals": rules}
    return parse(raw, root / "engkit.toml")


def _write(root: Path, rel: str, content: str) -> Path:
    """写夹具文件。

    Args:
        root: 仓库根。
        rel: 相对路径。
        content: 内容。

    Returns:
        Path: 写入路径。
    """
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _rules(**overrides: object) -> dict:
    """默认规则段（可覆盖）。

    Args:
        overrides: 覆盖项。

    Returns:
        dict: TOML 段。
    """
    base: dict = {"deny_globs": ["svc/**/*.py"], "literals": ["alpha", "beta"]}
    base.update(overrides)
    return base


def test_parse_full_fields(tmp_path: Path) -> None:
    """完整字段可解析。"""
    cfg = _cfg(
        tmp_path,
        _rules(
            enabled=False,
            allow_globs=["**/tests/**"],
            exclude_globs=["gen/**"],
            include_comments=False,
            combine_suffixes=["_"],
            baseline="deploy/baseline.json",
            baseline_ref="origin/main",
            exempt_marker="allow:",
            baseline_version=2,
            include_extensions=[".py"],
        ),
    )
    rule = cfg.product_literals_rules  # type: ignore[attr-defined]
    assert rule.enabled is False
    assert rule.allow_globs == ("**/tests/**",)
    assert rule.exclude_globs == ("gen/**",)
    assert rule.include_comments is False
    assert rule.combine_suffixes == ("_",)
    assert rule.baseline == "deploy/baseline.json"
    assert rule.baseline_ref == "origin/main"
    assert rule.exempt_marker == "allow:"
    assert rule.baseline_version == 2
    assert rule.include_extensions == (".py",)


def test_parse_defaults(tmp_path: Path) -> None:
    """未给可选字段时取缺省值。"""
    rule = _cfg(tmp_path, _rules()).product_literals_rules  # type: ignore[attr-defined]
    assert rule.enabled is True
    assert rule.include_comments is True
    assert rule.combine_suffixes == ("_", ".", ":", "-")
    assert rule.baseline == ""
    assert rule.baseline_ref == ""
    assert rule.exempt_marker == "product-literal:allow:"
    assert ".py" in rule.include_extensions


def test_parse_missing_literals(tmp_path: Path) -> None:
    """缺 `literals` 即报错。"""
    with pytest.raises(ConfigError):
        _cfg(tmp_path, {"deny_globs": ["svc/**/*.py"]})


def test_parse_empty_literals(tmp_path: Path) -> None:
    """空标识清单即报错（规则不得形同虚设）。"""
    with pytest.raises(ConfigError):
        _cfg(tmp_path, {"deny_globs": ["svc/**/*.py"], "literals": []})


def test_parse_unknown_key(tmp_path: Path) -> None:
    """未知键即报错。"""
    with pytest.raises(ConfigError):
        _cfg(tmp_path, _rules(unknown=1))


def test_parse_wrong_type(tmp_path: Path) -> None:
    """类型不符即报错。"""
    with pytest.raises(ConfigError):
        _cfg(tmp_path, _rules(enabled="yes"))


def test_glob_matches_forms() -> None:
    """`**/` / `**` / `*` / `?` / 字面 五类 glob 语义。"""
    assert pl.matches("backend/libs/a/b.py", ("backend/**/*.py",))
    assert pl.matches("backend/a.py", ("backend/**/*.py",))
    assert pl.matches("gen/deep/x.ts", ("gen/**",))
    assert pl.matches("svc/a.py", ("svc/*.py",))
    assert not pl.matches("svc/sub/a.py", ("svc/*.py",))
    assert pl.matches("web/a.ts", ("web/?.ts",))
    assert not pl.matches("web/ab.ts", ("web/?.ts",))
    assert pl.matches("svc/a.py", ("svc/a.py",))
    assert not pl.matches("svc/b.py", ("svc/a.py",))


def test_scan_roots_dedup() -> None:
    """扫描根取字面前缀并去重。"""
    assert pl.scan_roots(("svc/**/*.py", "svc/*.py", "web/?.ts", "**/*.md")) == ["svc", "web", "."]
    assert pl.scan_roots(("**/*.py",)) == ["."]


def test_accept_right() -> None:
    """右侧边界判定：词边界 / 组合后缀 / 标识符内。"""
    assert pl.accept_right("alpha", 5, ("_", ".", ":", "-"))
    assert not pl.accept_right("alphax", 5, ("_", ".", ":", "-"))
    assert pl.accept_right("alpha_dept", 5, ("_",))
    assert not pl.accept_right("alpha_dept", 5, (".",))
    assert pl.accept_right("alpha:query", 5, (":",))
    assert not pl.accept_right("alpha:query", 5, (".",))
    assert pl.accept_right("alpha x", 5, ("_",))
    assert not pl.accept_right("alpha-", 5, ("_",))


def test_python_comment_mask_marks_docstring_and_comment() -> None:
    """Python 注表面掩码覆盖 docstring 与 `#` 注释。"""
    source = '"""头部 docstring alpha"""\nX = "alpha"\n# alpha 注释\n'
    mask = pl.python_comment_mask(source)
    assert mask is not None
    assert mask[source.index("docstring alpha")] is True
    assert mask[source.index('"alpha"') + 1] is False
    assert mask[source.index("alpha 注释")] is True


def test_python_comment_mask_none_on_syntax_error() -> None:
    """语法不可解析返回 None（调用方降级）。"""
    assert pl.python_comment_mask("def broken(:\n") is None


def test_script_comment_mask_forms() -> None:
    """脚本注表面：行注释 / 块注释 / 未闭合 / 转义 / 反引号。"""
    source = (
        'const a = "alpha" // alpha\n'
        "/* alpha\n alpha */\n"
        'const b = "\\\\alpha"; // alpha\n'
        "const c = `alpha`; /* alpha\n"
    )
    mask = pl.script_comment_mask(source)
    lines = source.splitlines(keepends=True)
    offsets = pl._span_offsets(source)
    assert mask[offsets[0] + source.splitlines()[0].index("//")] is True
    assert mask[offsets[1]] is True
    assert mask[offsets[3] + lines[3].index("alpha")] is False
    assert mask[offsets[4] + lines[4].index("/*")] is True


def test_comment_mask_non_script_suffix() -> None:
    """非脚本体一律视为代码面。"""
    assert all(item is False for item in pl.comment_mask("doc.md", "alpha"))


def test_truncated_vue_like_source_downgrades() -> None:
    """`.py` 语法错误时降级为全文扫描并标降级。"""
    hits, degraded = pl.scan_source("svc/x.py", "def broken(:\n    alpha\n", _rule_simple())
    assert degraded is True
    assert [hit.literal for hit in hits] == ["alpha"]


def _rule_simple() -> object:
    """单标识规则对象（用例内构造，避免依赖配置文件）。"""
    return parse(
        {"docs": {"root": "docs"}, "rules": {"product_literals": {"deny_globs": ["svc/**"], "literals": ["alpha"]}}},
        Path("engkit.toml"),
    ).product_literals_rules


def test_iter_target_files_missing_root(tmp_path: Path) -> None:
    """扫描根不存在时返回空清单。"""
    cfg = _cfg(tmp_path, _rules(deny_globs=["nope/**/*.py"]))
    assert pl.iter_target_files(cfg, cfg.product_literals_rules) == []  # type: ignore[attr-defined]


def test_iter_target_files_dedup_and_filter(tmp_path: Path) -> None:
    """重叠 glob 去重、扩展名过滤、不匹配 glob 的文件不纳入。"""
    _write(tmp_path, "svc/a.py", "alpha\n")
    _write(tmp_path, "svc/b.txt", "alpha\n")
    _write(tmp_path, "svc/c.md", "alpha\n")
    cfg = _cfg(tmp_path, _rules(deny_globs=["svc/*.py", "svc/**/*.py"]))
    found = [path.name for path in pl.iter_target_files(cfg, cfg.product_literals_rules)]  # type: ignore[attr-defined]
    assert found == ["a.py"]


def test_collect_counts_and_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """允许 / 排除计数正确，读取异常记问题。"""
    _write(tmp_path, "svc/a.py", "alpha\n")
    _write(tmp_path, "svc/tests/test_a.py", "alpha\n")
    _write(tmp_path, "gen/b.py", "alpha\n")
    _write(tmp_path, "svc/bad.py", "alpha\n")
    cfg = _cfg(
        tmp_path, _rules(deny_globs=["svc/**/*.py", "gen/*.py"], allow_globs=["**/tests/**"], exclude_globs=["gen/**"])
    )
    original = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "bad.py":
            raise OSError("不可读")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    outcome = pl.Outcome()
    hits = pl.collect(cfg, cfg.product_literals_rules, outcome)  # type: ignore[attr-defined]
    assert [hit.file for hit in hits] == ["svc/a.py"]
    assert outcome.counts["allowed"] == 1
    assert outcome.counts["excluded"] == 1
    assert any("扫描异常" in problem for problem in outcome.problems)


def test_write_and_read_baseline_roundtrip(tmp_path: Path) -> None:
    """基线写入后读回一致（路径 + 指纹 + 计数）。"""
    cfg = _cfg(tmp_path, _rules(baseline="deploy/baseline.json"))
    rule = cfg.product_literals_rules  # type: ignore[attr-defined]
    hits = [pl.Hit("svc/a.py", 1, 'X = "alpha"', "alpha", False), pl.Hit("svc/a.py", 2, "alpha", "alpha", True)]
    path = tmp_path / "deploy/baseline.json"
    pl.write_baseline(path, hits, rule)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == rule.baseline_version
    assert payload["entries"][0]["samples"] == ["alpha"]
    assert pl.read_baseline(path, rule) == {
        ("svc/a.py", pl.fingerprint('X = "alpha"')): 1,
        ("svc/a.py", pl.fingerprint("alpha")): 1,
    }
    assert pl.read_baseline(tmp_path / "missing.json", rule) == {}


@pytest.mark.parametrize(
    "payload",
    [
        "{ not json",
        '{"version": 99, "entries": []}',
        '{"version": 1, "entries": {}}',
        '{"version": 1, "entries": [1]}',
        '{"version": 1, "entries": [{"path": "a"}]}',
        '{"version": 1, "entries": [{"path": "a", "fingerprint": "b", "count": 0}]}',
    ],
)
def test_parse_baseline_errors(tmp_path: Path, payload: str) -> None:
    """基线格式 / 版本 / 条目非法一律报错。"""
    rule = _cfg(tmp_path, _rules(baseline="b.json")).product_literals_rules  # type: ignore[attr-defined]
    path = tmp_path / "b.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(pl.BaselineError):
        pl.read_baseline(path, rule)


def test_check_reports_unreadable_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """基线读取抛 OSError 时记问题并中止比对。"""
    _write(tmp_path, "svc/a.py", "alpha\n")
    cfg = _cfg(tmp_path, _rules(baseline="deploy/b.json"))
    rule = cfg.product_literals_rules  # type: ignore[attr-defined]
    path = _write(tmp_path, "deploy/b.json", "{}")

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise OSError("不可读")
        return original_read(self, *args, **kwargs)

    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    outcome = pl.check(cfg, rule)
    assert any("基线非法" in problem for problem in outcome.problems)


def test_ref_baseline_invalid_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """基准基线内容非法时告警且跳过断言。"""
    cfg = _cfg(tmp_path, _rules(baseline="b.json", baseline_ref="origin/main"))
    rule = cfg.product_literals_rules  # type: ignore[attr-defined]
    monkeypatch.setattr(pl, "read_at", lambda *_args, **_kwargs: "{ not json")
    outcome = pl.Outcome()
    assert pl.ref_baseline(cfg, rule, outcome) is None
    assert outcome.warnings


def test_compare_baseline_growth_and_shrink() -> None:
    """只减不增：条目减少通过，条目增加 / 计数增加失败。"""
    key = ("svc/a.py", "abc")
    assert pl.compare_baseline({}, {key: 3}) == []
    assert pl.compare_baseline({key: 1}, {key: 1}) == []
    assert pl.compare_baseline({("x.py", "1"): 1}, {}) != []
    assert pl.compare_baseline({key: 2}, {key: 1}) != []


def test_run_missing_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """未登记规则段退 2。"""
    cfg = _cfg(tmp_path, None)
    assert pl.run(cfg, json_output=False, update_baseline=False, baseline_ref="", as_self_test=False) == 2
    assert "未登记规则" in capsys.readouterr().out


def test_run_disabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """enabled=false 直接退 0。"""
    cfg = _cfg(tmp_path, _rules(enabled=False))
    assert pl.run(cfg, json_output=False, update_baseline=False, baseline_ref="", as_self_test=False) == 0
    assert "未启用" in capsys.readouterr().out


def test_run_update_baseline(tmp_path: Path) -> None:
    """--update-baseline 写回基线并退 0。"""
    _write(tmp_path, "svc/a.py", "alpha\n")
    cfg = _cfg(tmp_path, _rules(baseline="deploy/b.json"))
    assert pl.run(cfg, json_output=False, update_baseline=True, baseline_ref="", as_self_test=False) == 0
    assert (tmp_path / "deploy/b.json").is_file()


def test_run_json_and_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """有新增退 1 且 JSON 可解析；吸收后（含 baseline-ref 覆盖）退 0。"""
    _write(tmp_path, "svc/a.py", "alpha\n")
    cfg = _cfg(tmp_path, _rules(baseline="deploy/b.json"))
    assert pl.run(cfg, json_output=True, update_baseline=False, baseline_ref="", as_self_test=False) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    pl.run(cfg, json_output=False, update_baseline=True, baseline_ref="", as_self_test=False)
    capsys.readouterr()
    assert pl.run(cfg, json_output=False, update_baseline=False, baseline_ref="no-such-ref", as_self_test=False) == 0
    assert "基准" in capsys.readouterr().out


def test_run_self_test_entry(tmp_path: Path) -> None:
    """`--self-test` 入口通过（覆盖内置矩阵）。"""
    cfg = _cfg(tmp_path, _rules())
    assert pl.run(cfg, json_output=False, update_baseline=False, baseline_ref="", as_self_test=True) == 0


def test_read_at_returns_content_and_none(tmp_path: Path) -> None:
    """git 引用读取：命中返回内容，缺失返回 None。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _write(tmp_path, "b.json", "{}")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    assert gitutil.read_at(tmp_path, "HEAD", "b.json") == "{}"
    assert gitutil.read_at(tmp_path, "HEAD", "missing.json") is None
    assert gitutil.read_at(tmp_path, "no-such-ref", "b.json") is None


def test_emit_text_and_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    """文本输出含汇总与告警行。"""
    outcome = pl.Outcome(
        counts={"files": 1, "excluded": 0, "allowed": 0, "total": 0, "added": 0, "baseline_entries": 0}
    )
    outcome.warnings.append("[降级] x.py")
    pl.emit(outcome, as_json=False)
    text = capsys.readouterr().out
    assert "通过" in text and "[降级] x.py" in text


def test_emit_text_with_problems(capsys: pytest.CaptureFixture[str]) -> None:
    """文本输出命中新增时列出问题行。"""
    outcome = pl.Outcome(counts={"files": 1, "total": 1, "added": 1})
    outcome.problems.append("[新增] svc/a.py:1 命中 1 处")
    pl.emit(outcome, as_json=False)
    text = capsys.readouterr().out
    assert "不通过" in text and "[新增]" in text


def test_scan_roots_literal_pattern(tmp_path: Path) -> None:
    """纯字面 glob 也派生扫描根（循环正常退出）。"""
    assert pl.scan_roots(("svc/a.py",)) == ["svc/a.py"]


def test_iter_target_files_nested_roots_and_unmatched(tmp_path: Path) -> None:
    """嵌套扫描根重叠时按已访问跳过；扩展名命中但 glob 不匹配的文件不纳入。"""
    _write(tmp_path, "svc/sub/a.py", "alpha\n")
    _write(tmp_path, "svc/note.ts", "alpha\n")
    cfg = _cfg(tmp_path, _rules(deny_globs=["svc/**/*.py", "svc/sub/*.py"]))
    found = [path.name for path in pl.iter_target_files(cfg, cfg.product_literals_rules)]  # type: ignore[attr-defined]
    assert found == ["a.py"]


def test_scan_source_without_marker_and_right_boundary(tmp_path: Path) -> None:
    """未配置豁免标记时不查找标记；右侧为标识符字符的命中被丢弃。"""
    rule = _cfg(tmp_path, _rules(exempt_marker="")).product_literals_rules  # type: ignore[attr-defined]
    hits, degraded = pl.scan_source("svc/a.py", 'A = "alpha"\nB = "alphaX"\n', rule)  # type: ignore[arg-type]
    assert [hit.line_no for hit in hits] == [1]
    assert degraded is False


def test_scan_source_case_sensitive(tmp_path: Path) -> None:
    """标识按配置字面大小写匹配（`Alpha` 不算命中）。"""
    rule = _cfg(tmp_path, _rules()).product_literals_rules  # type: ignore[attr-defined]
    hits, _ = pl.scan_source("svc/a.py", 'A = "Alpha"\nB = "alpha"\n', rule)  # type: ignore[arg-type]
    assert [hit.line_no for hit in hits] == [2]


def test_check_without_baseline_reports_added(tmp_path: Path) -> None:
    """未配置基线时命中一律计新增。"""
    _write(tmp_path, "svc/a.py", "alpha\n")
    cfg = _cfg(tmp_path, _rules())
    outcome = pl.check(cfg, cfg.product_literals_rules)  # type: ignore[attr-defined]
    assert outcome.counts["added"] == 1
    assert outcome.problems


def test_check_baseline_ref_compare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置了基准时执行只减不增比对。"""
    _write(tmp_path, "svc/a.py", "alpha\n")
    cfg = _cfg(tmp_path, _rules(baseline="deploy/b.json", baseline_ref="origin/main"))
    rule = cfg.product_literals_rules  # type: ignore[attr-defined]
    pl.write_baseline(tmp_path / "deploy/b.json", pl.collect(cfg, rule, pl.Outcome()), rule)  # type: ignore[arg-type]
    monkeypatch.setattr(pl, "ref_baseline", lambda *_args, **_kwargs: {})
    outcome = pl.check(cfg, rule)  # type: ignore[arg-type]
    assert any("只减不增" in problem for problem in outcome.problems)


def test_self_test_failure_path(tmp_path: Path) -> None:
    """自检夹具被削弱时自检失败（覆盖失败分支）。"""
    rule = _cfg(tmp_path, _rules()).product_literals_rules  # type: ignore[attr-defined]
    import dataclasses

    broken = dataclasses.replace(rule, include_extensions=(".md",))  # type: ignore[arg-type]
    assert pl.self_test(broken) == 1  # type: ignore[arg-type]
