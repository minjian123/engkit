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
class DevloopStep:
    """开发环的一条附加步骤（护栏 / 静态检查；命令由仓配置声明）。

    Attributes:
        label: 步骤名（输出用）。
        cmd: 命令模板；支持占位 `{python}` / `{root}` / `{changed}`（全量改动清单文件）/
            `{changed_py}`（只含 .py 的清单文件）/ `{changed_py*}`（展开为多个 .py 文件路径）。
        only_if: 执行条件；`changed_py` 表示「本次改动含 .py 才跑」，空串表示总跑。
    """

    label: str
    cmd: tuple[str, ...]
    only_if: str = ""


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
        test_cmd: 单包测试命令模板（`{package}` 替换为包名）。
    """

    root: str
    groups: tuple[str, ...] = ("packages", "apps", "modules")
    workspace_protocol: str = "workspace:"
    test_cmd: str = "pnpm --filter {package} test"


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
class PreflightGateConfig:
    """预检场景闸门与调用审计（`[preflight.gate]`）。

    Attributes:
        dirty_tree: 脏工作树即拒绝（预检是推送前门禁；可在仓配置关闭）。
        audit_file: 调用审计落点（相对仓根；空串表示不记录）。
    """

    dirty_tree: bool = True
    audit_file: str = ".cache/preflight/audit.jsonl"


@dataclass(frozen=True)
class PreflightConfig:
    """预检配置。

    Attributes:
        cwd: 缺省工作目录（相对仓根）。
        since: 变更基准（git 引用）。
        budget: 预算护栏。
        gate: 场景闸门与调用审计。
        steps: 步骤（按声明序执行）。
        self_tests: 护栏自测。
    """

    cwd: str = "."
    since: str = "origin/main"
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    gate: PreflightGateConfig = field(default_factory=PreflightGateConfig)
    steps: tuple[Step, ...] = ()
    self_tests: tuple[SelfTest, ...] = ()


@dataclass(frozen=True)
class PushConfig:
    """推送入口配置（`[push]`）：预检是推送的内部步骤，不再是独立命令。

    Attributes:
        remote: 远端名。
        branch: 目标分支（空串取当前分支）。
        doc_globs: 视为文档的文件名模式；待推提交全为文档时自动降为快速档。
    """

    remote: str = "origin"
    branch: str = ""
    doc_globs: tuple[str, ...] = (
        "*.md",
        "*.txt",
        "*.png",
        "*.jpg",
        "*.jpeg",
        "*.gif",
        "*.svg",
        "*.webp",
        "*.pdf",
    )


@dataclass(frozen=True)
class RunOnceConfig:
    """重命令回放配置。

    Attributes:
        cache_dir: 缓存目录（相对仓根）。
        window_seconds: 回放窗口（秒）。
    """

    cache_dir: str = ".cache/run_once"
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
class EolConfig:
    """行尾护栏配置（`[eol]`）。

    Attributes:
        skip_prefixes: 不纳入检查的路径前缀（相对仓库根）。
        include_suffixes: 只检查这些后缀（为空表示不按后缀过滤）。
    """

    skip_prefixes: tuple[str, ...] = ()
    include_suffixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolchainConfig:
    """工具链基线登记（`[toolchain]`）。

    Attributes:
        baseline: ruff 规则基线路径（相对仓根；各接入仓以 `extend` 指向本文件）。
        ruff_configs: 需校验「只 extend、不复制规则段」的仓配置路径（相对仓根；
            指向平级仓的路径用 `../` 表示；路径不存在即跳过，供 CI 单仓克隆场景使用）。
    """

    baseline: str = "toolchain/ruff-base.toml"
    ruff_configs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DesignRegistryRules:
    """数据库设计登记规则（`[rules.base.design]`）。

    Attributes:
        dir: 数据库设计目录（相对仓根）。
        overview: 总览文件（相对仓根）。
        table_subdir: 数据表文件子目录名。
        dialect_subdir: 方言特性子目录名。
        status_values: 表状态合法取值。
    """

    dir: str
    overview: str
    table_subdir: str
    dialect_subdir: str
    status_values: tuple[str, ...]


@dataclass(frozen=True)
class NamingRules:
    """登记面命名形态规则（`[rules.base.naming]`）。

    Attributes:
        tool_root: 工具链根（目录名与脚本名纳入登记面）。
        package_roots: 前端包根清单。
        versions_root: 表结构变更清单根（可空）。
    """

    tool_root: str = ""
    package_roots: tuple[str, ...] = ()
    versions_root: str = ""


@dataclass(frozen=True)
class RuntimeIdentifierRule:
    """运行期登记值来源（`[[rules.base.runtime]]`）。

    Attributes:
        path: 来源文件（相对仓根）。
        extract: 提取方式（`widget_literal` / `ts_widgets` / `code_pattern` / `icon_pattern`）。
        expect_count: 期望提取条数下限。
    """

    path: str
    extract: str
    expect_count: int = 1


@dataclass(frozen=True)
class BaseRules:
    """基座完整性校验规则（`[rules.base]`）。

    Attributes:
        manifest: 基座文档清单（相对仓根）。
        base_dirs: 基座扫描目录清单。
        cross_layer: 禁止被基座文件相对链接的前缀。
        local_only: 清单登记但不参与校验的前缀。
        bad_wording: 项目专属措辞残留词表。
        runtime: 运行期登记值来源。
        design: 数据库设计登记规则（可空）。
        naming: 登记面命名形态规则（可空）。
        link_suffixes: 参与跨层检查的链接后缀。
        section_ref_root: 跨文档编号引用扫描根（空串取文档根）。
    """

    manifest: str
    base_dirs: tuple[str, ...]
    cross_layer: tuple[str, ...]
    local_only: tuple[str, ...] = ()
    bad_wording: tuple[str, ...] = ()
    runtime: tuple[RuntimeIdentifierRule, ...] = ()
    design: DesignRegistryRules | None = None
    naming: NamingRules | None = None
    link_suffixes: tuple[str, ...] = (".md", ".html")
    section_ref_root: str = ""


@dataclass(frozen=True)
class BackendBaseRules:
    """后端基类体系校验规则（`[rules.backend_base]`）。

    Attributes:
        manifest: 基类清单（相对仓根）。
        chain_doc: 继承链与体系根清单文档（相对仓根）。
        source_dirs: 后端源码扫描面。
        error_codes: 错误码清单模块。
        versions_dir: 迁移清单根。
        direct_baseline: 直继承存量基线 JSON。
        root_bases_marker: 体系根清单小节标记。
        value_object_root: 值对象体系根类名。
        framework_object_root: 框架对象体系根类名。
        datasources: 合法数据源段。
        default_chain: 缺省链名。
        domain_layout: 域布局 JSON（迁移清单域目录据此校验）。
    """

    manifest: str
    chain_doc: str
    source_dirs: tuple[str, ...]
    error_codes: str
    versions_dir: str
    direct_baseline: str
    root_bases_marker: str
    value_object_root: str
    framework_object_root: str
    datasources: tuple[str, ...]
    default_chain: str
    domain_layout: str = ""


@dataclass(frozen=True)
class DocsStatusRules:
    """需求 / 任务 / 计划一致性规则（`[rules.docs_status]`）。

    Attributes:
        project_dir: 阶段根目录（相对仓根）。
        stage: 缺省核对的阶段目录名。
        statuses: 状态合法取值。
        todo_statuses: 剩余任务表允许的状态。
        done_status: 已完成状态取值（完成日期口径据此判定）。
        req_id_pattern: 需求编号形态。
        task_id_pattern: 嵌套子任务编号形态。
        plan_id_pattern: 计划编号形态。
        overview_glob: 需求总览文件形态。
        plan_glob: 计划文件形态。
        aid_dirs: 任务目录内的附属目录（不计为任务）。
        plan_done_columns: 已完成任务表列数。
        plan_todo_columns: 剩余任务表列数。
        record_dirs: 已完成任务的留痕目录要求。
    """

    project_dir: str
    stage: str
    statuses: tuple[str, ...]
    todo_statuses: tuple[str, ...]
    req_id_pattern: str
    task_id_pattern: str
    plan_id_pattern: str
    overview_glob: str = "00_需求_*.md"
    plan_glob: str = "01_计划_*.md"
    aid_dirs: tuple[str, ...] = ("设计", "测试", "实施")
    plan_done_columns: int = 6
    plan_todo_columns: int = 8
    record_dirs: tuple[str, ...] = ("实施",)
    done_status: str = "已完成"


@dataclass(frozen=True)
class RegistrySpec:
    """一份 AST 登记清单的定位（模块 / 常量 / 枚举 / 字段名）。

    Attributes:
        path: 模块文件（相对仓根）。
        const: 清单常量名。
        enum: `kind` 取值枚举名（可空）。
        key_field: 标识字段名。
        table_field: 表名字段名（可空）。
        kind_field: kind 字段名。
    """

    path: str
    const: str
    enum: str = ""
    key_field: str = ""
    table_field: str = ""
    kind_field: str = "kind"


@dataclass(frozen=True)
class SystemFieldsRules:
    """系统字段（`form_key`）校验规则（`[rules.system_fields]`）。

    Attributes:
        catalog: 表单清单定位。
        datasource: 数据源清单定位。
        table_dir: 数据表文件目录（相对仓根）。
        row_form_key: 表文件必须出现的 `form_key` 行名。
        row_parent_id: 表文件必须出现的父级行名。
    """

    catalog: RegistrySpec
    datasource: RegistrySpec
    table_dir: str
    row_form_key: str = "form_key"
    row_parent_id: str = "parent_id"


@dataclass(frozen=True)
class EnvTemplateRule:
    """一份环境变量模板（`[[rules.env_example.templates]]`）。

    Attributes:
        path: 模板路径（相对仓根）。
        required_keys: 必须齐备的键位。
        removed_keys: 不得残留的废弃键（与段级清单取并集）。
    """

    path: str
    required_keys: tuple[str, ...] = ()
    removed_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvExampleRules:
    """环境变量模板键位规则（`[rules.env_example]`）。

    Attributes:
        templates: 模板清单。
        removed_keys: 段级废弃键（对全部模板生效）。
        private_key_marker: 疑似真实私钥标记。
        placeholder_marker: 占位符标记（含标记即视为占位）。
    """

    templates: tuple[EnvTemplateRule, ...]
    removed_keys: tuple[str, ...] = ()
    private_key_marker: str = "PRIVATE KEY"
    placeholder_marker: str = "..."


@dataclass(frozen=True)
class PrototypeRules:
    """界面「实现 ↔ 原型」对照规则（`[rules.prototype]`）。

    Attributes:
        record_pattern: 测试记录路径正则（相对仓根）。
        context_pattern: 原型依据候选文本路径正则。
        prototype_ref_pattern: 原型依据引用正则。
        ui_patterns: 界面源码路径正则。
        section_heading: 原型对照节标题正则。
        table_header_keyword: 对照表表头须含的关键词。
        screenshot_pattern: 截图引用正则。
        unfinished_words: 未完成词表。
        not_applicable_word: 「不适用」判定词。
        min_reason_len: 「不适用」理由最小长度。
        base: 变更比较基准。
        context_dirs: 任务目录下收文本的子目录。
    """

    record_pattern: str
    context_pattern: str
    prototype_ref_pattern: str
    ui_patterns: tuple[str, ...]
    section_heading: str
    table_header_keyword: str
    screenshot_pattern: str
    unfinished_words: tuple[str, ...]
    not_applicable_word: str
    min_reason_len: int = 10
    base: str = "origin/main"
    context_dirs: tuple[str, ...] = ("设计", "实施", "测试")


@dataclass(frozen=True)
class ServiceBoundariesRules:
    """服务边界与跨库访问规则（`[rules.service_boundaries]`）。

    Attributes:
        shared_groups: 共享库分组目录（相对后端根）。
        service_groups: 服务分组目录（相对后端根）。
        shared_libs: 共享库包名清单。
        service_packages: 服务包全集。
        runtime_packages: 任意服务镜像恒含的包。
        layers_shared: 共享库分层反向依赖（源子包 → 禁止目标）。
        layers_service: 服务内分层反向依赖。
        ownership_module: 表归属登记模块（AST 解析）。
        ownership_const: 表归属清单常量名。
        owner_every_service: 每服务自有哨兵。
        infrastructure_tables: 基础设施表清单。
        exempt_modules: 表名引用扫描豁免模块（相对仓根后缀匹配）。
        catalog_module: 服务目录模块。
        catalog_const: 服务目录常量名。
        layout: 域布局 JSON。
        exceptions: 读侧例外 JSON。
        baseline: 域级基线 JSON。
        shared_models_marker: 共享模型子包标记。
        ops_dir: 运维脚本目录（相对仓根）。
        ops_dockerfile: 镜像构建文件（相对仓根）。
        run_image_values: `RUN_IMAGE` 合法取值。
        image_packages: 镜像 → 所含包集合。
        counters: 分类计数键清单。
        no_domain_tables: 不归业务域的表（`domain` 恒空）。
        package_prefix: 服务包名前缀（去前缀得服务标识）。
        shared_lib_service: 共享库包归属的服务标识。
        domain_subdir: 域子包目录名（`domains/<域>/<层>`）。
        shared_table_prefixes: 平台共享表前缀（按表级归属判定、不判前缀冲突）。
        incremental_full_when: 增量扫描升为全量的路径 glob（生成物 / 清单目录等仓特有触发面；
            布局 / 基线 / 例外 / 归属登记 / 目录登记由规则字段自身推出，不必在此重复）。
        table_index_cache: 表声明索引缓存文件（相对仓根）；空串表示不落盘（每次现扫）。
        public_export_baseline: 域公共出口白名单与递减基线 JSON（相对仓根）；
            空串表示停用出口断言（产品仓未按域分包时）。
    """

    shared_groups: tuple[str, ...]
    service_groups: tuple[str, ...]
    shared_libs: tuple[str, ...]
    service_packages: tuple[str, ...]
    runtime_packages: tuple[str, ...]
    layers_shared: dict[str, tuple[str, ...]]
    layers_service: dict[str, tuple[str, ...]]
    ownership_module: str
    ownership_const: str
    owner_every_service: str
    infrastructure_tables: tuple[str, ...]
    exempt_modules: tuple[str, ...]
    catalog_module: str
    catalog_const: str
    layout: str
    exceptions: str
    baseline: str
    shared_models_marker: str
    ops_dir: str
    ops_dockerfile: str
    run_image_values: tuple[str, ...]
    image_packages: dict[str, tuple[str, ...]]
    counters: tuple[str, ...]
    no_domain_tables: tuple[str, ...] = ()
    package_prefix: str = ""
    shared_lib_service: str = ""
    domain_subdir: str = "domains"
    shared_table_prefixes: tuple[str, ...] = ()
    incremental_full_when: tuple[str, ...] = ()
    table_index_cache: str = ""
    public_export_baseline: str = ""


@dataclass(frozen=True)
class BareCollectionsRules:
    """集合声明白名单规则（`[rules.bare_collections]`）。

    Attributes:
        scan_targets: 扫描根清单（相对仓根）。
        whitelist: 集合声明白名单（命中即放行）。
        banned_bare: 裸容器形态。
        banned_readonly: 只读 / 可变抽象落点。
        banned_ascending: 升序形态。
        unregistered: 未登记形态集。
        tuple_forms: 元组形态。
        protocol_forms: 迭代与调用协议（不查）。
        implementation_files: 集合体系实现文件（不属约束对象）。
        allow_marker: 行级豁免标记。
        baseline: 基线快照路径。
        baseline_version: 基线格式版本。
        reasons: 违规原因提示（按命中形态）。
        exclude_parts: 剪枝目录名。
    """

    scan_targets: tuple[str, ...]
    whitelist: tuple[str, ...]
    banned_bare: tuple[str, ...]
    banned_readonly: tuple[str, ...]
    banned_ascending: tuple[str, ...]
    unregistered: tuple[str, ...]
    tuple_forms: tuple[str, ...]
    protocol_forms: tuple[str, ...]
    implementation_files: tuple[str, ...]
    allow_marker: str
    baseline: str
    baseline_version: int = 1
    reasons: dict[str, str] = field(default_factory=dict)
    exclude_parts: tuple[str, ...] = ("__pycache__", ".venv", "node_modules", ".git")


@dataclass(frozen=True)
class MetricsRules:
    """边界度量采集规则（`[rules.metrics]`）。

    Attributes:
        violation_keys: 越界计数键清单（总越界数 = 各键求和）。
        cross_access_key: 跨库访问计数键。
        registered_exceptions_key: 例外登记条数键。
        metric_name: 运行期指标名。
        checker: 静态校验来源子命令（固定值，键位保留）。
    """

    violation_keys: tuple[str, ...]
    cross_access_key: str
    registered_exceptions_key: str
    metric_name: str
    checker: str = "service-boundaries"


@dataclass(frozen=True)
class ProductLiteralsRules:
    """产品字面量护栏规则（`[rules.product_literals]`）。

    Attributes:
        enabled: 是否启用（产品仓可显式关闭）。
        deny_globs: 禁入面（相对仓根的 posix glob；命中即违规）。
        allow_globs: 允许面（合规出现位置，优先于禁入面判定）。
        exclude_globs: 排除面（生成物 / 构建产物，不进扫描与统计）。
        literals: 需拦截的产品标识清单。
        include_comments: 注释与 docstring 是否计入。
        combine_suffixes: 组合形态后缀（标识紧邻这些字符仍算违规）。
        baseline: 存量基线 JSON 路径。
        baseline_ref: 只减不增断言的比较基准（git ref；空串不校验）。
        exempt_marker: 行内豁免标记前缀（理由必须非空）。
        baseline_version: 基线格式版本。
        include_extensions: 参与扫描的扩展名（按文件后缀过滤）。
    """

    deny_globs: tuple[str, ...]
    literals: tuple[str, ...]
    enabled: bool = True
    allow_globs: tuple[str, ...] = ()
    exclude_globs: tuple[str, ...] = ()
    include_comments: bool = True
    combine_suffixes: tuple[str, ...] = ("_", ".", ":", "-")
    baseline: str = ""
    baseline_ref: str = ""
    exempt_marker: str = "product-literal:allow:"
    baseline_version: int = 1
    include_extensions: tuple[str, ...] = (".py", ".ts", ".tsx", ".vue", ".js", ".mjs")


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
        push: 推送入口配置。
        runonce: 回放配置。
        guard: 重命令拦截配置。
        kiwi: 用例登记配置。
        eol: 行尾护栏配置。
        toolchain: 工具链基线登记。
        base_rules: 基座完整性规则（未登记为 None）。
        backend_base_rules: 后端基类体系规则（未登记为 None）。
        boundaries_rules: 服务边界规则（未登记为 None）。
        collections_rules: 集合声明规则（未登记为 None）。
        system_fields_rules: 系统字段规则（未登记为 None）。
        prototype_rules: 原型对照规则（未登记为 None）。
        env_example_rules: 环境模板规则（未登记为 None）。
        docs_status_rules: 文档一致性规则（未登记为 None）。
        metrics_rules: 边界度量规则（未登记为 None）。
        product_literals_rules: 产品字面量护栏规则（未登记为 None）。
        base_threshold: 基础模块绝对阈值。
        base_ratio: 基础模块占比阈值。
        depth: 选测依赖深度（`closure` 传递闭包 / `direct` 直接依赖）。
        ruff_cmd: ruff 执行器前缀（开发环只查改动文件；空串表示跳过）。
        ruff_config: ruff 配置路径（相对仓根；空串不传 `--config`）。
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
    push: PushConfig = field(default_factory=PushConfig)
    eol: EolConfig = field(default_factory=EolConfig)
    toolchain: ToolchainConfig = field(default_factory=ToolchainConfig)
    base_rules: BaseRules | None = None
    backend_base_rules: BackendBaseRules | None = None
    boundaries_rules: ServiceBoundariesRules | None = None
    collections_rules: BareCollectionsRules | None = None
    system_fields_rules: SystemFieldsRules | None = None
    prototype_rules: PrototypeRules | None = None
    env_example_rules: EnvExampleRules | None = None
    docs_status_rules: DocsStatusRules | None = None
    metrics_rules: MetricsRules | None = None
    product_literals_rules: ProductLiteralsRules | None = None
    base_threshold: int = 100
    base_ratio: float = 0.6
    depth: str = "closure"
    ruff_cmd: str = ""
    ruff_config: str = ""
    backend_global: GlobalRule = field(default_factory=GlobalRule)
    backend_extra: tuple[ExtraTests, ...] = ()
    frontend_global: GlobalRule = field(default_factory=GlobalRule)
    devloop_steps: tuple[DevloopStep, ...] = ()

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
    "eol",
    "push",
    "toolchain",
    "devloop",
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


def _str_map(source: dict[str, Any], key: str, where: str) -> dict[str, str]:
    """取字符串映射（`[段.<键>]` 下的标量表）。

    Args:
        source: 表。
        key: 键名。
        where: 位置描述。

    Returns:
        dict[str, str]: 映射。

    Raises:
        ConfigError: 类型不符或取值不是字符串。
    """
    table = _table(source, key, f"{where}.{key}")
    result: dict[str, str] = {}
    for name, value in table.items():
        if not isinstance(value, str):
            raise _fail(f"{where}.{key}", f"`{name}` 应为字符串")
        result[name] = value
    return result


def _list_map(source: dict[str, Any], key: str, where: str) -> dict[str, tuple[str, ...]]:
    """取「字符串数组」映射（`[段.<键>]` 下每键一个数组）。

    Args:
        source: 表。
        key: 键名。
        where: 位置描述。

    Returns:
        dict[str, tuple[str, ...]]: 映射。

    Raises:
        ConfigError: 类型不符或元素不是字符串。
    """
    table = _table(source, key, f"{where}.{key}")
    result: dict[str, tuple[str, ...]] = {}
    for name, value in table.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise _fail(f"{where}.{key}", f"`{name}` 应为字符串数组")
        result[name] = tuple(value)
    return result


def _parse_design(table: dict[str, Any], where: str) -> DesignRegistryRules:
    """解析数据库设计登记规则。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        DesignRegistryRules: 规则。
    """
    _check_keys(table, ("dir", "overview", "table_subdir", "dialect_subdir", "status_values"), where)
    return DesignRegistryRules(
        dir=_string(table, "dir", where),
        overview=_string(table, "overview", where),
        table_subdir=_string(table, "table_subdir", where, "数据表设计"),
        dialect_subdir=_string(table, "dialect_subdir", where, "方言特性"),
        status_values=_strings(table, "status_values", where),
    )


def _parse_naming(table: dict[str, Any], where: str) -> NamingRules:
    """解析登记面命名形态规则。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        NamingRules: 规则。
    """
    _check_keys(table, ("tool_root", "package_roots", "versions_root"), where)
    return NamingRules(
        tool_root=_string(table, "tool_root", where, ""),
        package_roots=_strings(table, "package_roots", where),
        versions_root=_string(table, "versions_root", where, ""),
    )


def _parse_runtime(table: dict[str, Any], where: str) -> tuple[RuntimeIdentifierRule, ...]:
    """解析运行期登记值来源清单。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        tuple[RuntimeIdentifierRule, ...]: 规则清单。

    Raises:
        ConfigError: 提取方式非法。
    """
    rules: list[RuntimeIdentifierRule] = []
    for index, item in enumerate(_tables(table, "runtime", where)):
        item_where = f"{where}.runtime[{index}]"
        _check_keys(item, ("path", "extract", "expect_count"), item_where)
        extract = _string(item, "extract", item_where)
        if extract not in ("widget_literal", "ts_widgets", "code_pattern", "icon_pattern"):
            raise _fail(item_where, f"`extract` 非法：{extract}")
        rules.append(
            RuntimeIdentifierRule(
                path=_string(item, "path", item_where),
                extract=extract,
                expect_count=int(_scalar(item, "expect_count", item_where, int, 1)),
            )
        )
    return tuple(rules)


def _parse_base_rules(table: dict[str, Any], where: str) -> BaseRules:
    """解析 `[rules.base]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        BaseRules: 规则。
    """
    _check_keys(
        table,
        (
            "manifest",
            "base_dirs",
            "cross_layer",
            "local_only",
            "bad_wording",
            "runtime",
            "design",
            "naming",
            "link_suffixes",
            "section_ref_root",
        ),
        where,
    )
    design_table = _table(table, "design", f"{where}.design")
    naming_table = _table(table, "naming", f"{where}.naming")
    return BaseRules(
        manifest=_string(table, "manifest", where),
        base_dirs=_strings(table, "base_dirs", where),
        cross_layer=_strings(table, "cross_layer", where),
        local_only=_strings(table, "local_only", where),
        bad_wording=_strings(table, "bad_wording", where),
        runtime=_parse_runtime(table, where),
        design=_parse_design(design_table, f"{where}.design") if design_table else None,
        naming=_parse_naming(naming_table, f"{where}.naming") if naming_table else None,
        link_suffixes=_strings(table, "link_suffixes", where, (".md", ".html")),
        section_ref_root=_string(table, "section_ref_root", where, ""),
    )


def _parse_backend_base_rules(table: dict[str, Any], where: str) -> BackendBaseRules:
    """解析 `[rules.backend_base]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        BackendBaseRules: 规则。
    """
    _check_keys(
        table,
        (
            "manifest",
            "chain_doc",
            "source_dirs",
            "error_codes",
            "versions_dir",
            "direct_baseline",
            "root_bases_marker",
            "value_object_root",
            "framework_object_root",
            "datasources",
            "default_chain",
            "domain_layout",
        ),
        where,
    )
    return BackendBaseRules(
        manifest=_string(table, "manifest", where),
        chain_doc=_string(table, "chain_doc", where),
        source_dirs=_strings(table, "source_dirs", where),
        error_codes=_string(table, "error_codes", where),
        versions_dir=_string(table, "versions_dir", where),
        direct_baseline=_string(table, "direct_baseline", where),
        root_bases_marker=_string(table, "root_bases_marker", where),
        value_object_root=_string(table, "value_object_root", where),
        framework_object_root=_string(table, "framework_object_root", where),
        datasources=_strings(table, "datasources", where),
        default_chain=_string(table, "default_chain", where),
        domain_layout=_string(table, "domain_layout", where, ""),
    )


def _parse_docs_status_rules(table: dict[str, Any], where: str) -> DocsStatusRules:
    """解析 `[rules.docs_status]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        DocsStatusRules: 规则。
    """
    _check_keys(
        table,
        (
            "project_dir",
            "stage",
            "statuses",
            "todo_statuses",
            "req_id_pattern",
            "task_id_pattern",
            "plan_id_pattern",
            "overview_glob",
            "plan_glob",
            "aid_dirs",
            "plan_done_columns",
            "plan_todo_columns",
            "record_dirs",
            "done_status",
        ),
        where,
    )
    return DocsStatusRules(
        project_dir=_string(table, "project_dir", where),
        stage=_string(table, "stage", where),
        statuses=_strings(table, "statuses", where),
        todo_statuses=_strings(table, "todo_statuses", where),
        req_id_pattern=_string(table, "req_id_pattern", where),
        task_id_pattern=_string(table, "task_id_pattern", where),
        plan_id_pattern=_string(table, "plan_id_pattern", where),
        overview_glob=_string(table, "overview_glob", where, "00_需求_*.md"),
        plan_glob=_string(table, "plan_glob", where, "01_计划_*.md"),
        aid_dirs=_strings(table, "aid_dirs", where, ("设计", "测试", "实施")),
        plan_done_columns=int(_scalar(table, "plan_done_columns", where, int, 6)),
        plan_todo_columns=int(_scalar(table, "plan_todo_columns", where, int, 8)),
        record_dirs=_strings(table, "record_dirs", where, ("实施",)),
        done_status=_string(table, "done_status", where, "已完成"),
    )


def _parse_registry_spec(table: dict[str, Any], where: str) -> RegistrySpec:
    """解析一份 AST 登记清单定位。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        RegistrySpec: 定位。
    """
    _check_keys(table, ("path", "const", "enum", "key_field", "table_field", "kind_field"), where)
    return RegistrySpec(
        path=_string(table, "path", where),
        const=_string(table, "const", where),
        enum=_string(table, "enum", where, ""),
        key_field=_string(table, "key_field", where),
        table_field=_string(table, "table_field", where, ""),
        kind_field=_string(table, "kind_field", where, "kind"),
    )


def _parse_system_fields_rules(table: dict[str, Any], where: str) -> SystemFieldsRules:
    """解析 `[rules.system_fields]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        SystemFieldsRules: 规则。
    """
    _check_keys(table, ("catalog", "datasource", "table_dir", "row_form_key", "row_parent_id"), where)
    return SystemFieldsRules(
        catalog=_parse_registry_spec(_table(table, "catalog", f"{where}.catalog"), f"{where}.catalog"),
        datasource=_parse_registry_spec(_table(table, "datasource", f"{where}.datasource"), f"{where}.datasource"),
        table_dir=_string(table, "table_dir", where),
        row_form_key=_string(table, "row_form_key", where, "form_key"),
        row_parent_id=_string(table, "row_parent_id", where, "parent_id"),
    )


def _parse_env_example_rules(table: dict[str, Any], where: str) -> EnvExampleRules:
    """解析 `[rules.env_example]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        EnvExampleRules: 规则。

    Raises:
        ConfigError: 未登记任何模板。
    """
    _check_keys(table, ("templates", "removed_keys", "private_key_marker", "placeholder_marker"), where)
    templates: list[EnvTemplateRule] = []
    for index, item in enumerate(_tables(table, "templates", where)):
        item_where = f"{where}.templates[{index}]"
        _check_keys(item, ("path", "required_keys", "removed_keys"), item_where)
        templates.append(
            EnvTemplateRule(
                path=_string(item, "path", item_where),
                required_keys=_strings(item, "required_keys", item_where),
                removed_keys=_strings(item, "removed_keys", item_where),
            )
        )
    if not templates:
        raise _fail(where, "需给出 `templates`（至少一份模板）")
    return EnvExampleRules(
        templates=tuple(templates),
        removed_keys=_strings(table, "removed_keys", where),
        private_key_marker=_string(table, "private_key_marker", where, "PRIVATE KEY"),
        placeholder_marker=_string(table, "placeholder_marker", where, "..."),
    )


def _parse_prototype_rules(table: dict[str, Any], where: str) -> PrototypeRules:
    """解析 `[rules.prototype]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        PrototypeRules: 规则。
    """
    _check_keys(
        table,
        (
            "record_pattern",
            "context_pattern",
            "prototype_ref_pattern",
            "ui_patterns",
            "section_heading",
            "table_header_keyword",
            "screenshot_pattern",
            "unfinished_words",
            "not_applicable_word",
            "min_reason_len",
            "base",
            "context_dirs",
        ),
        where,
    )
    return PrototypeRules(
        record_pattern=_string(table, "record_pattern", where),
        context_pattern=_string(table, "context_pattern", where),
        prototype_ref_pattern=_string(table, "prototype_ref_pattern", where),
        ui_patterns=_strings(table, "ui_patterns", where),
        section_heading=_string(table, "section_heading", where),
        table_header_keyword=_string(table, "table_header_keyword", where, "结论"),
        screenshot_pattern=_string(table, "screenshot_pattern", where),
        unfinished_words=_strings(table, "unfinished_words", where),
        not_applicable_word=_string(table, "not_applicable_word", where, "不适用"),
        min_reason_len=int(_scalar(table, "min_reason_len", where, int, 10)),
        base=_string(table, "base", where, "origin/main"),
        context_dirs=_strings(table, "context_dirs", where, ("设计", "实施", "测试")),
    )


def _parse_boundaries_rules(table: dict[str, Any], where: str) -> ServiceBoundariesRules:
    """解析 `[rules.service_boundaries]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        ServiceBoundariesRules: 规则。
    """
    _check_keys(
        table,
        (
            "shared_groups",
            "service_groups",
            "shared_libs",
            "service_packages",
            "runtime_packages",
            "layers",
            "ownership",
            "catalog",
            "layout",
            "exceptions",
            "baseline",
            "shared_models_marker",
            "ops",
            "counters",
            "package_prefix",
            "shared_lib_service",
            "domain_subdir",
            "shared_table_prefixes",
            "incremental_full_when",
            "table_index_cache",
            "public_export_baseline",
        ),
        where,
    )
    layers = _table(table, "layers", f"{where}.layers")
    _check_keys(layers, ("shared", "service"), f"{where}.layers")
    ownership = _table(table, "ownership", f"{where}.ownership")
    _check_keys(
        ownership,
        ("module", "const", "owner_every_service", "infrastructure_tables", "exempt_modules", "no_domain_tables"),
        f"{where}.ownership",
    )
    catalog = _table(table, "catalog", f"{where}.catalog")
    _check_keys(catalog, ("module", "const"), f"{where}.catalog")
    ops = _table(table, "ops", f"{where}.ops")
    _check_keys(ops, ("dir", "dockerfile", "run_image_values", "image_packages"), f"{where}.ops")
    return ServiceBoundariesRules(
        shared_groups=_strings(table, "shared_groups", where, ("libs",)),
        service_groups=_strings(table, "service_groups", where, ("services",)),
        shared_libs=_strings(table, "shared_libs", where),
        service_packages=_strings(table, "service_packages", where),
        runtime_packages=_strings(table, "runtime_packages", where),
        layers_shared=_list_map(layers, "shared", f"{where}.layers"),
        layers_service=_list_map(layers, "service", f"{where}.layers"),
        ownership_module=_string(ownership, "module", f"{where}.ownership"),
        ownership_const=_string(ownership, "const", f"{where}.ownership"),
        owner_every_service=_string(ownership, "owner_every_service", f"{where}.ownership"),
        infrastructure_tables=_strings(ownership, "infrastructure_tables", f"{where}.ownership"),
        exempt_modules=_strings(ownership, "exempt_modules", f"{where}.ownership"),
        catalog_module=_string(catalog, "module", f"{where}.catalog"),
        catalog_const=_string(catalog, "const", f"{where}.catalog"),
        layout=_string(table, "layout", where),
        exceptions=_string(table, "exceptions", where),
        baseline=_string(table, "baseline", where),
        shared_models_marker=_string(table, "shared_models_marker", where, ""),
        ops_dir=_string(ops, "dir", f"{where}.ops", ""),
        ops_dockerfile=_string(ops, "dockerfile", f"{where}.ops", ""),
        run_image_values=_strings(ops, "run_image_values", f"{where}.ops"),
        image_packages=_list_map(ops, "image_packages", f"{where}.ops"),
        counters=_strings(table, "counters", where),
        no_domain_tables=_strings(ownership, "no_domain_tables", f"{where}.ownership"),
        package_prefix=_string(table, "package_prefix", where, ""),
        shared_lib_service=_string(table, "shared_lib_service", where, ""),
        domain_subdir=_string(table, "domain_subdir", where, "domains"),
        shared_table_prefixes=_strings(table, "shared_table_prefixes", where),
        incremental_full_when=_strings(table, "incremental_full_when", where),
        table_index_cache=_string(table, "table_index_cache", where, ""),
        public_export_baseline=_string(table, "public_export_baseline", where, ""),
    )


def _parse_collections_rules(table: dict[str, Any], where: str) -> BareCollectionsRules:
    """解析 `[rules.bare_collections]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        BareCollectionsRules: 规则。
    """
    _check_keys(
        table,
        (
            "scan_targets",
            "whitelist",
            "banned",
            "unregistered",
            "tuple_forms",
            "protocol_forms",
            "implementation_files",
            "allow_marker",
            "baseline",
            "baseline_version",
            "reasons",
            "exclude_parts",
        ),
        where,
    )
    banned = _table(table, "banned", f"{where}.banned")
    _check_keys(banned, ("bare", "readonly", "ascending"), f"{where}.banned")
    return BareCollectionsRules(
        scan_targets=_strings(table, "scan_targets", where),
        whitelist=_strings(table, "whitelist", where),
        banned_bare=_strings(banned, "bare", f"{where}.banned"),
        banned_readonly=_strings(banned, "readonly", f"{where}.banned"),
        banned_ascending=_strings(banned, "ascending", f"{where}.banned"),
        unregistered=_strings(table, "unregistered", where),
        tuple_forms=_strings(table, "tuple_forms", where),
        protocol_forms=_strings(table, "protocol_forms", where),
        implementation_files=_strings(table, "implementation_files", where),
        allow_marker=_string(table, "allow_marker", where),
        baseline=_string(table, "baseline", where),
        baseline_version=int(_scalar(table, "baseline_version", where, int, 1)),
        reasons=_str_map(table, "reasons", where),
        exclude_parts=_strings(table, "exclude_parts", where, ("__pycache__", ".venv", "node_modules", ".git")),
    )


def _parse_metrics_rules(table: dict[str, Any], where: str) -> MetricsRules:
    """解析 `[rules.metrics]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        MetricsRules: 规则。
    """
    _check_keys(
        table,
        ("violation_keys", "cross_access_key", "registered_exceptions_key", "metric_name", "checker"),
        where,
    )
    return MetricsRules(
        violation_keys=_strings(table, "violation_keys", where),
        cross_access_key=_string(table, "cross_access_key", where),
        registered_exceptions_key=_string(table, "registered_exceptions_key", where),
        metric_name=_string(table, "metric_name", where),
        checker=_string(table, "checker", where, "service-boundaries"),
    )


def _parse_product_literals_rules(table: dict[str, Any], where: str) -> ProductLiteralsRules:
    """解析 `[rules.product_literals]`。

    Args:
        table: 子表。
        where: 位置描述。

    Returns:
        ProductLiteralsRules: 规则。

    Raises:
        ConfigError: 标识清单为空或字段非法。
    """
    _check_keys(
        table,
        (
            "enabled",
            "deny_globs",
            "allow_globs",
            "exclude_globs",
            "literals",
            "include_comments",
            "combine_suffixes",
            "baseline",
            "baseline_ref",
            "exempt_marker",
            "baseline_version",
            "include_extensions",
        ),
        where,
    )
    literals = _strings(table, "literals", where)
    if not literals:
        raise _fail(where, "`literals` 不得为空（否则规则形同虚设）")
    return ProductLiteralsRules(
        deny_globs=_strings(table, "deny_globs", where),
        literals=literals,
        enabled=_scalar(table, "enabled", where, bool, True),
        allow_globs=_strings(table, "allow_globs", where),
        exclude_globs=_strings(table, "exclude_globs", where),
        include_comments=_scalar(table, "include_comments", where, bool, True),
        combine_suffixes=_strings(table, "combine_suffixes", where, ("_", ".", ":", "-")),
        baseline=_string(table, "baseline", where, ""),
        baseline_ref=_string(table, "baseline_ref", where, ""),
        exempt_marker=_string(table, "exempt_marker", where, "product-literal:allow:"),
        baseline_version=int(_scalar(table, "baseline_version", where, int, 1)),
        include_extensions=_strings(table, "include_extensions", where, (".py", ".ts", ".tsx", ".vue", ".js", ".mjs")),
    )


def _parse_devloop_steps(raw: dict[str, Any]) -> tuple[DevloopStep, ...]:
    """解析 `[[devloop.steps]]`（缺省空表）。

    Args:
        raw: TOML 映射。

    Returns:
        tuple[DevloopStep, ...]: 步骤清单。

    Raises:
        ConfigError: 结构非法（缺 label / cmd 非字符串数组 / only_if 取值不支持）。
    """
    section = raw.get("devloop")
    if section is None:
        return ()
    if not isinstance(section, dict):
        raise ConfigError("[devloop] 必须是表")
    raw_steps = section.get("steps", [])
    if not isinstance(raw_steps, list):
        raise ConfigError("[devloop].steps 必须是表数组")
    steps: list[DevloopStep] = []
    for index, item in enumerate(raw_steps, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"[devloop].steps 第 {index} 项必须是表")
        label = item.get("label")
        cmd = item.get("cmd")
        only_if = item.get("only_if", "")
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"[devloop].steps 第 {index} 项缺 label")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(token, str) for token in cmd):
            raise ConfigError(f"[devloop].steps 第 {index} 项 cmd 必须是非空字符串数组")
        if not isinstance(only_if, str) or only_if not in ("", "changed_py"):
            raise ConfigError(f"[devloop].steps 第 {index} 项 only_if 只支持 changed_py")
        steps.append(DevloopStep(label=label, cmd=tuple(cmd), only_if=only_if))
    return tuple(steps)


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
        _check_keys(frontend_table, ("root", "groups", "workspace_protocol", "test_cmd"), "frontend")
        frontend = FrontendConfig(
            root=_string(frontend_table, "root", "frontend"),
            groups=_strings(frontend_table, "groups", "frontend", ("packages", "apps", "modules")),
            workspace_protocol=_string(frontend_table, "workspace_protocol", "frontend", "workspace:"),
            test_cmd=_string(frontend_table, "test_cmd", "frontend", "pnpm --filter {package} test"),
        )

    selection = _table(raw, "selection", "selection")
    _check_keys(selection, ("base_threshold", "base_ratio", "depth", "ruff_cmd", "ruff_config"), "selection")
    depth = _string(selection, "depth", "selection", "closure")
    if depth not in ("closure", "direct"):
        raise ConfigError(f"`selection.depth` 取值非法：{depth!r}（可选 closure / direct）")

    backend_global = GlobalRule()
    backend_extra: tuple[ExtraTests, ...] = ()
    frontend_global = GlobalRule()
    rules = _table(raw, "rules", "rules")
    base_rule_set: BaseRules | None = None
    backend_base_rule_set: BackendBaseRules | None = None
    boundaries_rule_set: ServiceBoundariesRules | None = None
    collections_rule_set: BareCollectionsRules | None = None
    system_fields_rule_set: SystemFieldsRules | None = None
    prototype_rule_set: PrototypeRules | None = None
    env_example_rule_set: EnvExampleRules | None = None
    docs_status_rule_set: DocsStatusRules | None = None
    metrics_rule_set: MetricsRules | None = None
    product_literals_rule_set: ProductLiteralsRules | None = None
    check_rule_keys = (
        "backend",
        "frontend",
        "base",
        "backend_base",
        "service_boundaries",
        "bare_collections",
        "system_fields",
        "prototype",
        "env_example",
        "docs_status",
        "metrics",
        "product_literals",
    )
    _check_keys(rules, check_rule_keys, "rules")
    if "base" in rules:
        base_rule_set = _parse_base_rules(_table(rules, "base", "rules.base"), "rules.base")
    if "backend_base" in rules:
        backend_base_rule_set = _parse_backend_base_rules(
            _table(rules, "backend_base", "rules.backend_base"), "rules.backend_base"
        )
    if "service_boundaries" in rules:
        boundaries_rule_set = _parse_boundaries_rules(
            _table(rules, "service_boundaries", "rules.service_boundaries"), "rules.service_boundaries"
        )
    if "bare_collections" in rules:
        collections_rule_set = _parse_collections_rules(
            _table(rules, "bare_collections", "rules.bare_collections"), "rules.bare_collections"
        )
    if "system_fields" in rules:
        system_fields_rule_set = _parse_system_fields_rules(
            _table(rules, "system_fields", "rules.system_fields"), "rules.system_fields"
        )
    if "prototype" in rules:
        prototype_rule_set = _parse_prototype_rules(_table(rules, "prototype", "rules.prototype"), "rules.prototype")
    if "env_example" in rules:
        env_example_rule_set = _parse_env_example_rules(
            _table(rules, "env_example", "rules.env_example"), "rules.env_example"
        )
    if "docs_status" in rules:
        docs_status_rule_set = _parse_docs_status_rules(
            _table(rules, "docs_status", "rules.docs_status"), "rules.docs_status"
        )
    if "metrics" in rules:
        metrics_rule_set = _parse_metrics_rules(_table(rules, "metrics", "rules.metrics"), "rules.metrics")
    if "product_literals" in rules:
        product_literals_rule_set = _parse_product_literals_rules(
            _table(rules, "product_literals", "rules.product_literals"), "rules.product_literals"
        )
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
    _check_keys(preflight_table, ("cwd", "since", "budget", "gate", "steps", "self_tests"), "preflight")
    budget_table = _table(preflight_table, "budget", "preflight.budget")
    _check_keys(budget_table, ("scoped", "full"), "preflight.budget")
    gate_table = _table(preflight_table, "gate", "preflight.gate")
    _check_keys(gate_table, ("dirty_tree", "audit_file"), "preflight.gate")
    preflight_config = PreflightConfig(
        cwd=_string(preflight_table, "cwd", "preflight", "."),
        since=_string(preflight_table, "since", "preflight", "origin/main"),
        budget=BudgetConfig(
            scoped=_floats(budget_table, "scoped", "preflight.budget", (300.0, 420.0)),
            full=_floats(budget_table, "full", "preflight.budget", (900.0, 1800.0)),
        ),
        gate=PreflightGateConfig(
            dirty_tree=bool(_scalar(gate_table, "dirty_tree", "preflight.gate", bool, True)),
            audit_file=_string(gate_table, "audit_file", "preflight.gate", ".cache/preflight/audit.jsonl"),
        ),
        steps=_parse_steps(preflight_table, "preflight"),
        self_tests=_parse_self_tests(preflight_table, "preflight"),
    )

    runonce_table = _table(raw, "runonce", "runonce")
    _check_keys(runonce_table, ("cache_dir", "window_seconds"), "runonce")
    runonce = RunOnceConfig(
        cache_dir=_string(runonce_table, "cache_dir", "runonce", ".cache/run_once"),
        window_seconds=int(_scalar(runonce_table, "window_seconds", "runonce", int, 1800)),
    )

    push_table = _table(raw, "push", "push")
    _check_keys(push_table, ("remote", "branch", "doc_globs"), "push")
    push = PushConfig(
        remote=_string(push_table, "remote", "push", "origin"),
        branch=_string(push_table, "branch", "push", ""),
        doc_globs=_strings(push_table, "doc_globs", "push") or PushConfig().doc_globs,
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

    eol_table = _table(raw, "eol", "eol")
    _check_keys(eol_table, ("skip_prefixes", "include_suffixes"), "eol")
    eol = EolConfig(
        skip_prefixes=_strings(eol_table, "skip_prefixes", "eol"),
        include_suffixes=_strings(eol_table, "include_suffixes", "eol"),
    )

    toolchain_table = _table(raw, "toolchain", "toolchain")
    _check_keys(toolchain_table, ("baseline", "ruff_configs"), "toolchain")
    toolchain = ToolchainConfig(
        baseline=_string(toolchain_table, "baseline", "toolchain", "toolchain/ruff-base.toml"),
        ruff_configs=_strings(toolchain_table, "ruff_configs", "toolchain"),
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
        push=push,
        eol=eol,
        toolchain=toolchain,
        base_rules=base_rule_set,
        backend_base_rules=backend_base_rule_set,
        boundaries_rules=boundaries_rule_set,
        collections_rules=collections_rule_set,
        system_fields_rules=system_fields_rule_set,
        prototype_rules=prototype_rule_set,
        env_example_rules=env_example_rule_set,
        docs_status_rules=docs_status_rule_set,
        metrics_rules=metrics_rule_set,
        product_literals_rules=product_literals_rule_set,
        base_threshold=int(_scalar(selection, "base_threshold", "selection", int, 100)),
        base_ratio=float(_scalar(selection, "base_ratio", "selection", float, 0.6)),
        depth=depth,
        ruff_cmd=_string(selection, "ruff_cmd", "selection", ""),
        ruff_config=_string(selection, "ruff_config", "selection", ""),
        backend_global=backend_global,
        backend_extra=backend_extra,
        frontend_global=frontend_global,
        devloop_steps=_parse_devloop_steps(raw),
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
