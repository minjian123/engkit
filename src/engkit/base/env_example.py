"""环境变量模板键位门禁（`engkit base env-example`）。

口径：模板必须与配置实现键位一致——声明的键位齐备、已移除键不得残留、占位示例不得误贴真实密钥。
模板清单与键位全部来自 `[rules.env_example]`，不再固定「哪两份文件」。
"""

from __future__ import annotations

from ..config import Config, EnvExampleRules, EnvTemplateRule
from ..console import out
from .rules import RulesError, read_text, require

MIN_KEYS = ("templates",)
"""未登记时报错给出的最小键指引。"""


def check_texts(rule: EnvExampleRules, texts: dict[str, str]) -> tuple[str, ...]:
    """比对模板文本，返回问题清单（纯函数，便于自检）。

    Args:
        rule: 规则段。
        texts: 模板路径 → 全文。

    Returns:
        tuple[str, ...]: 问题描述（空表示通过）。
    """
    problems: list[str] = []
    all_removed = dict.fromkeys(rule.removed_keys)
    for template in rule.templates:
        for key in template.removed_keys:
            all_removed.setdefault(key, None)
    for label, text in texts.items():
        for key in all_removed:
            if key in text:
                problems.append(f"{label} 残留已移除键 {key}")
    for template in rule.templates:
        text = texts.get(template.path, "")
        for key in template.required_keys:
            if key not in text:
                problems.append(f"{template.path} 缺键位 {key}")
    for label, text in texts.items():
        for number, line in enumerate(text.splitlines(), start=1):
            if rule.private_key_marker in line and rule.placeholder_marker not in line:
                problems.append(f"{label}:{number} 疑似真实私钥（占位示例须含 `{rule.placeholder_marker}`）")
    return tuple(problems)


def load_texts(cfg: Config, rule: EnvExampleRules) -> dict[str, str]:
    """读取规则声明的全部模板。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        dict[str, str]: 模板路径 → 全文。

    Raises:
        RulesError: 模板文件缺失。
    """
    missing = [item.path for item in rule.templates if not (cfg.root / item.path).is_file()]
    if missing:
        raise RulesError(f"模板文件缺失：{'、'.join(missing)}")
    return {item.path: read_text(cfg.root / item.path) for item in rule.templates}


def self_test(rule: EnvExampleRules) -> int:
    """自检：正常模板通过；废弃键残留 / 缺键 / 真实私钥三类反例被拦截。

    Args:
        rule: 规则段（向量取自本仓登记模板与键位）。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    good = {item.path: "\n".join(item.required_keys) for item in rule.templates}
    cases: list[tuple[str, dict[str, str], int]] = [
        ("正常", good, 0),
        (
            "废弃键残留",
            {**good, rule.templates[0].path: good[rule.templates[0].path] + "\n" + _removed(rule)},
            1,
        ),
        ("缺键", _drop_key(good, rule.templates[0]), 1),
        ("真实私钥", _add_private(good, rule.templates[0].path, rule.private_key_marker), 1),
    ]
    bad = 0
    for title, texts, expect in cases:
        found = check_texts(rule, texts)
        flag = "OK" if len(found) == expect else "FAIL"
        bad += flag == "FAIL"
        out(f"  [{flag}] {title}：期望 {expect} 项，实得 {len(found)} 项")
        for item in found:
            out(f"        {item}")
    out(f"\n[check_env_example self-test] {'通过' if not bad else '不通过'}：断言用例 {len(cases)} 个")
    return 1 if bad else 0


def _removed(rule: EnvExampleRules) -> str:
    """取一条废弃键文本（自检夹具）。

    Args:
        rule: 规则段。

    Returns:
        str: `键=值` 文本。
    """
    key = rule.removed_keys[0] if rule.removed_keys else "APP__ALGORITHM"
    return f"{key}=HS256"


def _drop_key(texts: dict[str, str], template: EnvTemplateRule) -> dict[str, str]:
    """删掉首个必填键（自检夹具）。

    Args:
        texts: 模板文本。
        template: 目标模板。

    Returns:
        dict[str, str]: 改后文本。
    """
    key = template.required_keys[0] if template.required_keys else ""
    return {**texts, template.path: texts[template.path].replace(key, "")}


def _add_private(texts: dict[str, str], label: str, marker: str) -> dict[str, str]:
    """加入一行疑似真实私钥（自检夹具）。

    Args:
        texts: 模板文本。
        label: 目标模板路径。
        marker: 私钥标记。

    Returns:
        dict[str, str]: 改后文本。
    """
    return {**texts, label: texts[label] + f"\n-----BEGIN {marker}-----"}


def run(cfg: Config, *, as_self_test: bool) -> int:
    """执行环境模板校验。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。

    Returns:
        int: 退出码（0 通过 / 1 有问题 / 2 规则或模板缺失）。
    """
    try:
        rule = require(cfg.env_example_rules, "env_example", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_env_example] {exc}")
        return 2
    if as_self_test:
        return self_test(rule)
    try:
        texts = load_texts(cfg, rule)
    except RulesError as exc:
        out(f"[check_env_example] {exc}")
        return 2
    problems = check_texts(rule, texts)
    if problems:
        out(f"[check_env_example] 不通过：{len(problems)} 项")
        for item in problems:
            out(f"  - {item}")
        return 1
    out(f"[check_env_example] 通过：{len(rule.templates)} 份模板键位齐备、无废弃键与真实密钥")
    return 0
