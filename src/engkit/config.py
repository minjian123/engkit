"""配置解析（`engkit.toml`）：各仓仓根一份，包内不出现任何仓名 / 目录字面。

解析口径：缺段取缺省值、类型不符或未知键一律报 `ConfigError`（配置是唯一事实源，
写错就必须当场失败，不能靠缺省值掩盖）。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = "engkit.toml"
"""仓根配置文件名。"""


class ConfigError(Exception):
    """配置缺失、不可解析或结构非法。"""


@dataclass(frozen=True)
class ScopeRule:
    """一条文档口径规则。

    Attributes:
        pattern: 正则（在单行内搜索）。
        label: 命中时的违规类型。
    """

    pattern: str
    label: str


@dataclass(frozen=True)
class ScopeConfig:
    """文档口径护栏配置。

    Attributes:
        allow_marker: 行级豁免标记。
        whitelist_dirs: 文档根下的白名单目录前缀。
        whitelist_files: 文档根下的白名单文件（精确匹配）。
        plan_dir_pattern: 阶段计划目录正则（相对文档根）。
        extra_rules: 追加规则。
    """

    allow_marker: str = "docs-scope:allow"
    whitelist_dirs: tuple[str, ...] = ()
    whitelist_files: tuple[str, ...] = ()
    plan_dir_pattern: str = ""
    extra_rules: tuple[ScopeRule, ...] = ()


@dataclass(frozen=True)
class DocsConfig:
    """文档配置。

    Attributes:
        root: 文档根（相对仓根）。
        skip: 不参与校验的路径前缀（相对仓根）。
        scope: 口径护栏配置。
    """

    root: str
    skip: tuple[str, ...] = ()
    scope: ScopeConfig = field(default_factory=ScopeConfig)


@dataclass(frozen=True)
class BackendConfig:
    """后端工程配置。

    Attributes:
        root: 后端根（相对仓根）。
        groups: 工程分组目录（`<root>/<group>/<工程>/{src,tests}`）。
        test_prefix: 测试文件名前缀。
        projects: 全量档工程范围（相对后端根；供「新增工程必登记」对账）。
        coverage_packages: 全量档覆盖率采集范围（顶层包名）。
    """

    root: str
    groups: tuple[str, ...] = ("libs", "services")
    test_prefix: str = "test_"
    projects: tuple[str, ...] = ()
    coverage_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrontendConfig:
    """前端 pnpm 工程配置。

    Attributes:
        root: 前端根（相对仓根）。
        groups: 工程分组目录。
        workspace_protocol: 工作区依赖协议前缀。
    """

    root: str
    groups: tuple[str, ...] = ("packages", "apps", "modules")
    workspace_protocol: str = "workspace:"


@dataclass(frozen=True)
class GlobalRule:
    """全局变更规则（命中即退化全量）。

    Attributes:
        exact: 精确匹配路径。
        prefixes: 前缀匹配路径。
        config_prefix: 配置文件名前缀（与 `config_suffix` 同时命中才判全局）。
        config_suffix: 配置文件后缀。
    """

    exact: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    config_prefix: str = ""
    config_suffix: str = ""


@dataclass(frozen=True)
class ExtraTests:
    """特殊路径 → 必跑测试的映射规则。

    Attributes:
        match_prefix: 变更路径前缀。
        test_dir_suffix: 测试目录后缀（在全部测试文件中按该后缀筛选）。
        test_files: 显式测试文件（相对后端根）。
        mapping: （脚本名, 测试文件）精确映射。
        fallback_dir: 未登记脚本的兜底测试目录（相对后端根）。
        coverage: 命中该规则时是否仍计入覆盖率门禁。
    """

    match_prefix: str
    test_dir_suffix: str = ""
    test_files: tuple[str, ...] = ()
    mapping: tuple[tuple[str, str], ...] = ()
    fallback_dir: str = ""
    coverage: bool = True


@dataclass(frozen=True)
class CoverageConfig:
    """覆盖率采集与门禁配置。

    Attributes:
        data_env: 采集数据文件环境变量名。
        source_env: 采集范围环境变量名。
        fail_under: 门禁阈值（缺省 100，按行 + 分支判定）。
        plugin: 采集插件模块名（`-p` 参数）。
        pythonpath: 采集时追加到 `PYTHONPATH` 的目录（相对仓根）。
        data_file: 采集数据文件基址（相对仓根）。
        interpreter: 门禁解释器前缀（缺省与 pytest 命令相同）。
    """

    data_env: str = "ENGKIT_COVERAGE_DATA"
    source_env: str = "ENGKIT_COVERAGE_SOURCE"
    fail_under: float = 100.0
    plugin: str = ""
    pythonpath: tuple[str, ...] = ()
    data_file: str = ".coverage"
    interpreter: str = ""


@dataclass(frozen=True)
class PytestConfig:
    """pytest 执行配置。

    Attributes:
        cmd: 执行器前缀（空格分词）。
        cwd: 工作目录（相对仓根）。
        jobs_flag: 预检并行档位参数名（透传给子命令）。
        max_rounds: 覆盖率扩圈轮次上限。
    """

    cmd: str = "python -m pytest"
    cwd: str = "."
    jobs_flag: str = "--jobs"
    max_rounds: int = 2


@dataclass(frozen=True)
class Step:
    """预检步骤。

    Attributes:
        label: 步骤标签。
        type: 步骤类型（`command` / `scoped_tests` / `full_tests` / `project_tests`）。
        cmd: 命令（`command` 类型必填；支持 `{python}` / `{root}` / `{config}` 占位符）。
        cwd: 工作目录（相对仓根；空串取预检缺省）。
        preset: 生效档位（`always` / `scoped` / `fast` / `full`）。
        optional: 命令缺失时是否只跳过、不计失败。
    """

    label: str
    type: str = "command"
    cmd: tuple[str, ...] = ()
    cwd: str = ""
    preset: str = "always"
    optional: bool = False


@dataclass(frozen=True)
class SelfTest:
    """护栏自测步骤（按变更按需执行）。

    Attributes:
        label: 步骤标签。
        cmd: 命令。
        deps: 触发路径前缀（含脚本自身；变更命中任一前缀才执行）。
    """

    label: str
    cmd: tuple[str, ...]
    deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class BudgetConfig:
    """预算护栏（单步秒数, 总体秒数）。

    Attributes:
        scoped: 定向档预算。
        full: 全量档预算。
    """

    scoped: tuple[float, float] = (300.0, 420.0)
    full: tuple[float, float] = (900.0, 1800.0)


@dataclass(frozen=True)
class PreflightConfig:
    """预检配置。

    Attributes:
        cwd: 缺省工作目录（相对仓根）。
        since: 变更基准（git 引用）。
        budget: 预算护栏。
        steps: 步骤（按声明序执行）。
        self_tests: 护栏自测。
    """

    cwd: str = "."
    since: str = "origin/main"
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    steps: tuple[Step, ...] = ()
    self_tests: tuple[SelfTest, ...] = ()


@dataclass(frozen=True)
class RunOnceConfig:
    """重命令回放配置。

    Attributes:
        cache_dir: 缓存目录（相对仓根）。
        window_seconds: 回放窗口（秒）。
    """

    cache_dir: str = ".cache/run-once"
    window_seconds: int = 1800


@dataclass(frozen=True)
class GuardConfig:
    """重命令拦截配置。

    Attributes:
        scripts: 一律视为重命令的脚本名（如预检 / 基座校验入口）。
        repo_dirs: 仓库级目录名（扫描类命令参数指向这些即视为大范围）。
        dir_test_threshold: 目录判重阈值（目录下测试文件数超过即算重命令）。
    """

    scripts: tuple[str, ...] = ()
    repo_dirs: tuple[str, ...] = ()
    dir_test_threshold: int = 20


@dataclass(frozen=True)
class KiwiConfig:
    """Kiwi TCMS 登记配置。

    Attributes:
        env_file: 凭据文件（相对仓根；不入库）。
        host_env: 主机键名（取自环境变量或凭据文件）。
        user_env: SSH 账号键名。
        container: 容器名。
        manage: 容器内 `manage.py` 路径。
    """

    env_file: str = "deploy/.env"
    host_env: str = "KIWI_HOST"
    user_env: str = "KIWI_SSH_USER"
    container: str = "kiwi"
    manage: str = "/Kiwi/manage.py"


@dataclass(frozen=True)
class Config:
    """一份仓配置。

    Attributes:
        path: 配置文件路径。
        root: 仓库根（配置文件所在目录）。
        name: 仓库名（输出文案用）。
        docs: 文档配置。
        backend: 后端配置（无后端工程时为 None）。
        frontend: 前端配置（无前端工程时为 None）。
        coverage: 覆盖率配置。
        pytest: pytest 执行配置。
        preflight: 预检配置。
        runonce: 回放配置。
        guard: 重命令拦截配置。
        kiwi: 用例登记配置。
        base_threshold: 基础模块绝对阈值。
        base_ratio: 基础模块占比阈值。
        backend_global: 后端全局变更规则。
        backend_extra: 后端特殊路径规则。
        frontend_global: 前端全局变更规则。
    """

    path: Path
    root: Path
    name: str
    docs: DocsConfig
    backend: BackendConfig | None
    frontend: FrontendConfig | None
    coverage: CoverageConfig
    pytest: PytestConfig
    preflight: PreflightConfig
    runonce: RunOnceConfig
    guard: GuardConfig
    kiwi: KiwiConfig
    base_threshold: int = 100
    base_ratio: float = 0.6
    backend_global: GlobalRule = field(default_factory=GlobalRule)
    backend_extra: tuple[ExtraTests, ...] = ()
    frontend_global: GlobalRule = field(default_factory=GlobalRule)

    def resolve(self, rel: str) -> Path:
        """把仓根相对路径解析为绝对路径。

        Args:
            rel: 相对仓库根的路径。

        Returns:
            Path: 绝对路径。
        """
        return self.root / rel

    @property
    def docs_root(self) -> Path:
        """文档根绝对路径。"""
        return self.root / self.docs.root

    @property
    def backend_root(self) -> Path | None:
        """后端根绝对路径（未配置时为 None）。"""
        return None if self.backend is None else self.root / self.backend.root

    @property
    def frontend_root(self) -> Path | None:
        """前端根绝对路径（未配置时为 None）。"""
        return None if self.frontend is None else self.root / self.frontend.root


_TOP_KEYS = (
    "repo",
    "docs",
    "backend",
    "frontend",
    "selection",
    "rules",
    "coverage",
    "pytest",
    "preflight",
    "runonce",
    "guard",
    "kiwi",
)

_MISSING: Any = object()


def _fail(where: str, message: str) -> ConfigError:
    """构造配置错误。

    Args:
        where: 出错位置（如 `docs.scope`）。
        message: 错误说明。

    Returns:
        ConfigError: 异常对象。
    """
    return ConfigError(f"配置 {where}：{message}")


def _check_keys(table: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    """校验键集合（未知键即报错）。

    Args:
        table: 已解析的表。
        allowed: 允许的键。
        where: 位置描述。

    Raises:
        ConfigError: 存在未知键。
    """
    unknown = [key for key in table if key not in allowed]
    if unknown:
        raise _fail(where, f"未知键 {unknown}（可用：{list(allowed)}）")


def _table(source: dict[str, Any], key: str, where: str, *, required: bool = False) -> dict[str, Any]:
    """取子表。

    Args:
        source: 上一层表。
        key: 键名。
        where: 位置描述。
        required: 是否必需。

    Returns:
        dict[str, Any]: 子表（缺省为空表）。

    Raises:
        ConfigError: 必需但缺失，或类型不是表。
    """
    value = source.get(key)
    if value is None:
        if required:
            raise _fail(where, "缺少必需段")
        return {}
    if not isinstance(value, dict):
        raise _fail(where, "必须是表（`[段]` 形式）")
    return value


def _scalar(source: dict[str, Any], key: str, where: str, kind: type, default: Any = _MISSING) -> Any:
    """取标量值并校验类型。

    Args:
        source: 表。
        key: 键名。
        where: 位置描述。
        kind: 期望类型。
        default: 缺省值（`_MISSING` 表示必填）。

    Returns:
        Any: 取值。

    Raises:
        ConfigError: 缺失或类型不符。
    """
    if key not in source:
        if default is _MISSING:
            raise _fail(where, f"缺少必需键 `{key}`")
        return default
    value = source[key]
    if kind is not bool and isinstance(value, bool):
        raise _fail(where, f"`{key}` 类型应为 {kind.__name__}")
    if kind is float:
        if not isinstance(value, int | float):
            raise _fail(where, f"`{key}` 类型应为 float")
        return float(value)
    if not isinstance(value, kind):
        raise _fail(where, f"`{key}` 类型应为 {kind.__name__}")
    return value


def _string(source: dict[str, Any], key: str, where: str, default: Any = _MISSING) -> Any:
    """取字符串值。

    Args:
        source: 表。
        key: 键名。
        where: 位置描述。
        default: 缺省值。

    Returns:
        Any: 字符串取值。
    """
    return _scalar(source, key, where, str, default)


def _strings(source: dict[str, Any], key: str, where: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """取字符串数组。

    Args:
        source: 表。
        key: 键名。
        where: 位置描述。
        default: 缺省值。

    Returns:
        tuple[str, ...]: 字符串元组。

    Raises:
        ConfigError: 类型不符或元素不是字符串。
    """
    if key not in source:
        return default
    value = source[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(where, f"`{key}` 应为字符串数组")
    return tuple(value)


def _floats(source: dict[str, Any], key: str, where: str, default: tuple[float, float]) -> tuple[float, float]:
    """取二元数值数组（预算护栏）。

    Args:
        source: 表。
        key: 键名。
        where: 位置描述。
        default: 缺省值。

    Returns:
        tuple[float, float]: 两个数值。

    Raises:
        ConfigError: 类型不符或长度不是 2。
    """
    if key not in source:
        return default
    value = source[key]
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, int | float) for item in value):
        raise _fail(where, f"`{key}` 应为 [单步秒数, 总体秒数]")
    return (float(value[0]), float(value[1]))


def _tables(source: dict[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    """取表数组（`[[段]]`）。

    Args:
        source: 表。
        key: 键名。
        where: 位置描述。

    Returns:
        list[dict[str, Any]]: 子表清单。

    Raises:
        ConfigError: 类型不符。
    """
    if key not in source:
        return []
    value = source[key]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise _fail(where, f"`{key}` 应为表数组（`[[{key}]]` 形式）")
    return value


def _parse_scope(table: dict[str, Any], where: str) -> ScopeConfig:
    """解析 `[docs.scope]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        ScopeConfig: 口径护栏配置。
    """
    _check_keys(table, ("allow_marker", "whitelist_dirs", "whitelist_files", "plan_dir_pattern", "extra_rules"), where)
    rules: list[ScopeRule] = []
    for index, item in enumerate(_tables(table, "extra_rules", where)):
        item_where = f"{where}.extra_rules[{index}]"
        _check_keys(item, ("pattern", "label"), item_where)
        rules.append(
            ScopeRule(
                pattern=_string(item, "pattern", item_where),
                label=_string(item, "label", item_where),
            )
        )
    return ScopeConfig(
        allow_marker=_string(table, "allow_marker", where, "docs-scope:allow"),
        whitelist_dirs=_strings(table, "whitelist_dirs", where),
        whitelist_files=_strings(table, "whitelist_files", where),
        plan_dir_pattern=_string(table, "plan_dir_pattern", where, ""),
        extra_rules=tuple(rules),
    )


def _parse_global(table: dict[str, Any], where: str) -> GlobalRule:
    """解析全局变更规则。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        GlobalRule: 规则。
    """
    _check_keys(table, ("exact", "prefixes", "config_prefix", "config_suffix"), where)
    return GlobalRule(
        exact=_strings(table, "exact", where),
        prefixes=_strings(table, "prefixes", where),
        config_prefix=_string(table, "config_prefix", where, ""),
        config_suffix=_string(table, "config_suffix", where, ""),
    )


def _parse_extra(table: dict[str, Any], index: int, where: str) -> ExtraTests:
    """解析一条特殊路径规则。

    Args:
        table: 子表。
        index: 序号。
        where: 位置描述。

    Returns:
        ExtraTests: 规则。

    Raises:
        ConfigError: 缺少匹配方式。
    """
    item_where = f"{where}[{index}]"
    _check_keys(
        table,
        ("match_prefix", "test_dir_suffix", "test_files", "mapping", "fallback_dir", "coverage"),
        item_where,
    )
    mapping: list[tuple[str, str]] = []
    for position, entry in enumerate(_tables(table, "mapping", item_where)):
        entry_where = f"{item_where}.mapping[{position}]"
        _check_keys(entry, ("script", "test"), entry_where)
        mapping.append(
            (
                _string(entry, "script", entry_where),
                _string(entry, "test", entry_where),
            )
        )
    rule = ExtraTests(
        match_prefix=_string(table, "match_prefix", item_where),
        test_dir_suffix=_string(table, "test_dir_suffix", item_where, ""),
        test_files=_strings(table, "test_files", item_where),
        mapping=tuple(mapping),
        fallback_dir=_string(table, "fallback_dir", item_where, ""),
        coverage=bool(_scalar(table, "coverage", item_where, bool, True)),
    )
    if not (rule.test_dir_suffix or rule.test_files or rule.mapping):
        raise _fail(item_where, "需给出 test_dir_suffix / test_files / mapping 之一")
    return rule


def _parse_steps(table: dict[str, Any], where: str) -> tuple[Step, ...]:
    """解析预检步骤。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        tuple[Step, ...]: 步骤清单。

    Raises:
        ConfigError: 步骤类型或档位非法、命令缺失。
    """
    steps: list[Step] = []
    for index, item in enumerate(_tables(table, "steps", where)):
        item_where = f"{where}.steps[{index}]"
        _check_keys(item, ("label", "type", "cmd", "cwd", "preset", "optional"), item_where)
        kind = _string(item, "type", item_where, "command")
        if kind not in ("command", "scoped_tests", "full_tests", "project_tests"):
            raise _fail(item_where, f"`type` 非法：{kind}")
        preset = _string(item, "preset", item_where, "always")
        if preset not in ("always", "scoped", "fast", "full"):
            raise _fail(item_where, f"`preset` 非法：{preset}")
        command = _strings(item, "cmd", item_where)
        if kind == "command" and not command:
            raise _fail(item_where, "`command` 类型需给出 `cmd`")
        steps.append(
            Step(
                label=_string(item, "label", item_where),
                type=kind,
                cmd=command,
                cwd=_string(item, "cwd", item_where, ""),
                preset=preset,
                optional=bool(_scalar(item, "optional", item_where, bool, False)),
            )
        )
    return tuple(steps)


def _parse_self_tests(table: dict[str, Any], where: str) -> tuple[SelfTest, ...]:
    """解析护栏自测。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        tuple[SelfTest, ...]: 自测清单。
    """
    entries: list[SelfTest] = []
    for index, item in enumerate(_tables(table, "self_tests", where)):
        item_where = f"{where}.self_tests[{index}]"
        _check_keys(item, ("label", "cmd", "deps"), item_where)
        entries.append(
            SelfTest(
                label=_string(item, "label", item_where),
                cmd=_strings(item, "cmd", item_where),
                deps=_strings(item, "deps", item_where),
            )
        )
    return tuple(entries)


def parse(raw: dict[str, Any], path: Path) -> Config:
    """把已解析的 TOML 映射转成配置对象。

    Args:
        raw: TOML 映射。
        path: 配置文件路径（仓根取其所在目录）。

    Returns:
        Config: 配置对象。

    Raises:
        ConfigError: 结构非法。
    """
    _check_keys(raw, _TOP_KEYS, "根")
    root = path.resolve().parent

    repo = _table(raw, "repo", "repo")
    _check_keys(repo, ("name",), "repo")
    name = _string(repo, "name", "repo", root.name)

    docs_table = _table(raw, "docs", "docs", required=True)
    _check_keys(docs_table, ("root", "skip", "scope"), "docs")
    docs = DocsConfig(
        root=_string(docs_table, "root", "docs"),
        skip=_strings(docs_table, "skip", "docs"),
        scope=_parse_scope(_table(docs_table, "scope", "docs.scope"), "docs.scope"),
    )

    backend: BackendConfig | None = None
    if "backend" in raw:
        backend_table = _table(raw, "backend", "backend")
        _check_keys(backend_table, ("root", "groups", "test_prefix", "projects", "coverage_packages"), "backend")
        backend = BackendConfig(
            root=_string(backend_table, "root", "backend"),
            groups=_strings(backend_table, "groups", "backend", ("libs", "services")),
            test_prefix=_string(backend_table, "test_prefix", "backend", "test_"),
            projects=_strings(backend_table, "projects", "backend"),
            coverage_packages=_strings(backend_table, "coverage_packages", "backend"),
        )

    frontend: FrontendConfig | None = None
    if "frontend" in raw:
        frontend_table = _table(raw, "frontend", "frontend")
        _check_keys(frontend_table, ("root", "groups", "workspace_protocol"), "frontend")
        frontend = FrontendConfig(
            root=_string(frontend_table, "root", "frontend"),
            groups=_strings(frontend_table, "groups", "frontend", ("packages", "apps", "modules")),
            workspace_protocol=_string(frontend_table, "workspace_protocol", "frontend", "workspace:"),
        )

    selection = _table(raw, "selection", "selection")
    _check_keys(selection, ("base_threshold", "base_ratio"), "selection")

    backend_global = GlobalRule()
    backend_extra: tuple[ExtraTests, ...] = ()
    frontend_global = GlobalRule()
    rules = _table(raw, "rules", "rules")
    _check_keys(rules, ("backend", "frontend"), "rules")
    if "backend" in rules:
        backend_rules = _table(rules, "backend", "rules.backend")
        _check_keys(backend_rules, ("global", "extra_tests"), "rules.backend")
        backend_global = _parse_global(_table(backend_rules, "global", "rules.backend.global"), "rules.backend.global")
        backend_extra = tuple(
            _parse_extra(item, index, "rules.backend.extra_tests")
            for index, item in enumerate(_tables(backend_rules, "extra_tests", "rules.backend.extra_tests"))
        )
    if "frontend" in rules:
        frontend_rules = _table(rules, "frontend", "rules.frontend")
        _check_keys(frontend_rules, ("global",), "rules.frontend")
        frontend_global = _parse_global(
            _table(frontend_rules, "global", "rules.frontend.global"), "rules.frontend.global"
        )

    coverage_table = _table(raw, "coverage", "coverage")
    _check_keys(
        coverage_table,
        ("data_env", "source_env", "fail_under", "plugin", "pythonpath", "data_file", "interpreter"),
        "coverage",
    )
    coverage = CoverageConfig(
        data_env=_string(coverage_table, "data_env", "coverage", "ENGKIT_COVERAGE_DATA"),
        source_env=_string(coverage_table, "source_env", "coverage", "ENGKIT_COVERAGE_SOURCE"),
        fail_under=float(_scalar(coverage_table, "fail_under", "coverage", float, 100.0)),
        plugin=_string(coverage_table, "plugin", "coverage", ""),
        pythonpath=_strings(coverage_table, "pythonpath", "coverage"),
        data_file=_string(coverage_table, "data_file", "coverage", ".coverage"),
        interpreter=_string(coverage_table, "interpreter", "coverage", ""),
    )

    pytest_table = _table(raw, "pytest", "pytest")
    _check_keys(pytest_table, ("cmd", "cwd", "jobs_flag", "max_rounds"), "pytest")
    pytest_config = PytestConfig(
        cmd=_string(pytest_table, "cmd", "pytest", "python -m pytest"),
        cwd=_string(pytest_table, "cwd", "pytest", "."),
        jobs_flag=_string(pytest_table, "jobs_flag", "pytest", "--jobs"),
        max_rounds=int(_scalar(pytest_table, "max_rounds", "pytest", int, 2)),
    )

    preflight_table = _table(raw, "preflight", "preflight")
    _check_keys(preflight_table, ("cwd", "since", "budget", "steps", "self_tests"), "preflight")
    budget_table = _table(preflight_table, "budget", "preflight.budget")
    _check_keys(budget_table, ("scoped", "full"), "preflight.budget")
    preflight_config = PreflightConfig(
        cwd=_string(preflight_table, "cwd", "preflight", "."),
        since=_string(preflight_table, "since", "preflight", "origin/main"),
        budget=BudgetConfig(
            scoped=_floats(budget_table, "scoped", "preflight.budget", (300.0, 420.0)),
            full=_floats(budget_table, "full", "preflight.budget", (900.0, 1800.0)),
        ),
        steps=_parse_steps(preflight_table, "preflight"),
        self_tests=_parse_self_tests(preflight_table, "preflight"),
    )

    runonce_table = _table(raw, "runonce", "runonce")
    _check_keys(runonce_table, ("cache_dir", "window_seconds"), "runonce")
    runonce = RunOnceConfig(
        cache_dir=_string(runonce_table, "cache_dir", "runonce", ".cache/run-once"),
        window_seconds=int(_scalar(runonce_table, "window_seconds", "runonce", int, 1800)),
    )

    guard_table = _table(raw, "guard", "guard")
    _check_keys(guard_table, ("scripts", "repo_dirs", "dir_test_threshold"), "guard")
    guard = GuardConfig(
        scripts=_strings(guard_table, "scripts", "guard"),
        repo_dirs=_strings(guard_table, "repo_dirs", "guard"),
        dir_test_threshold=int(_scalar(guard_table, "dir_test_threshold", "guard", int, 20)),
    )

    kiwi_table = _table(raw, "kiwi", "kiwi")
    _check_keys(kiwi_table, ("env_file", "host_env", "user_env", "container", "manage"), "kiwi")
    kiwi = KiwiConfig(
        env_file=_string(kiwi_table, "env_file", "kiwi", "deploy/.env"),
        host_env=_string(kiwi_table, "host_env", "kiwi", "KIWI_HOST"),
        user_env=_string(kiwi_table, "user_env", "kiwi", "KIWI_SSH_USER"),
        container=_string(kiwi_table, "container", "kiwi", "kiwi"),
        manage=_string(kiwi_table, "manage", "kiwi", "/Kiwi/manage.py"),
    )

    return Config(
        path=path,
        root=root,
        name=name,
        docs=docs,
        backend=backend,
        frontend=frontend,
        coverage=coverage,
        pytest=pytest_config,
        preflight=preflight_config,
        runonce=runonce,
        guard=guard,
        kiwi=kiwi,
        base_threshold=int(_scalar(selection, "base_threshold", "selection", int, 100)),
        base_ratio=float(_scalar(selection, "base_ratio", "selection", float, 0.6)),
        backend_global=backend_global,
        backend_extra=backend_extra,
        frontend_global=frontend_global,
    )


def load(path: Path) -> Config:
    """读取并解析配置文件。

    Args:
        path: 配置文件路径。

    Returns:
        Config: 配置对象。

    Raises:
        ConfigError: 文件不可读或结构非法。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取配置失败：{path}（{exc}）") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置解析失败：{path}（{exc}）") from exc
    return parse(raw, path)


def discover(root: Path) -> Path | None:
    """在仓库根查找配置文件。

    Args:
        root: 仓库根目录。

    Returns:
        Path | None: 配置文件路径；不存在时为 None。
    """
    candidate = root / CONFIG_NAME
    return candidate if candidate.is_file() else None


def find_upward(start: Path, limit: int = 6) -> Path | None:
    """从起始目录向上查找配置文件（供钩子类入口在任意工作目录使用）。

    Args:
        start: 起始目录。
        limit: 最多上溯的层数。

    Returns:
        Path | None: 配置文件路径；找不到时为 None。
    """
    current = start.resolve()
    for _ in range(limit + 1):
        found = discover(current)
        if found is not None:
            return found
        if current.parent == current:
            return None
        current = current.parent
    return None


def load_for(root: Path, override: str = "") -> Config:
    """按仓库根加载配置（可用 `--config` 覆盖路径）。

    Args:
        root: 仓库根目录。
        override: 显式配置路径（空串表示按仓根查找）。

    Returns:
        Config: 配置对象。

    Raises:
        ConfigError: 找不到配置文件（提示新建）或结构非法。
    """
    if override:
        return load(Path(override))
    found = discover(root)
    if found is None:
        raise ConfigError(f"未找到 {CONFIG_NAME}：{root}（各仓仓根需放一份配置，见接入说明）")
    return load(found)
