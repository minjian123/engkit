"""变更影响分析选测器（`engkit selection`）：把「改了哪些文件」推导成「该跑哪些测试 / 哪些前端包」。

口径（《测试规范》§2.1 与任务 13_01 / 13_03）：

1. **双重收敛**：先按目录同构取候选（源包目录 ↔ 同构测试目录），再用 AST 导入图收敛到真实引用者；
2. **基础模块显式退化**：受影响测试超阈值即退化为工程级全量，并显式给出原因与命中工程；
3. **覆盖率门禁不降级**：覆盖率目标恒为变更的包内源文件，扩圈只扩大测试集；
4. **前端按包推导**：`workspace:` 依赖反向闭包 + 根级配置全包退化。

工程分组、全局变更规则、特殊路径映射、阈值全部来自仓配置（`engkit.toml`），包内无仓名字面。
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil
from .config import Config, ExtraTests

_EXCLUDED_PARTS = ("__pycache__", ".venv", "node_modules")
"""扫描源码 / 测试时跳过的目录名。"""


@dataclass(frozen=True)
class Project:
    """一个后端工程。

    Attributes:
        name: 工程名（源码根下首个含 `__init__.py` 的目录名）。
        src: 源码根（相对仓库根）。
        tests: 测试根（相对后端根）。
        packages: 源码根下的顶层包名。
    """

    name: str
    src: str
    tests: str
    packages: tuple[str, ...]


@dataclass(frozen=True)
class ProjectMetrics:
    """逐工程选测度量。

    Attributes:
        name: 工程名。
        picked: 选中测试文件数。
        total: 工程测试文件总数。
        degraded: 是否退化为该工程全量。
    """

    name: str
    picked: int
    total: int
    degraded: bool


@dataclass(frozen=True)
class Selection:
    """后端选测结果。

    Attributes:
        test_paths: 选中的测试文件（相对后端根）。
        cov_modules: 覆盖率目标模块（只含变更的包内源文件）。
        degraded: 是否发生工程级全量退化。
        reason: 退化主因（空串表示未退化）。
        picked: 选中测试文件数。
        total: 全部工程测试文件总数。
        projects: 逐工程度量。
        notes: 逐条说明。
    """

    test_paths: tuple[str, ...] = ()
    cov_modules: tuple[str, ...] = ()
    degraded: bool = False
    reason: str = ""
    picked: int = 0
    total: int = 0
    projects: tuple[ProjectMetrics, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrontendPackage:
    """一个前端 pnpm 工程。

    Attributes:
        name: 包名。
        path: 工程根（相对仓库根）。
        workspace_deps: 依赖的工作区包名。
    """

    name: str
    path: str
    workspace_deps: tuple[str, ...]


@dataclass(frozen=True)
class FrontendSelection:
    """前端选测结果。

    Attributes:
        packages: 受影响包名（按声明序）。
        degraded: 是否退化为全部包。
        reason: 退化主因。
        notes: 逐条说明。
    """

    packages: tuple[str, ...] = ()
    degraded: bool = False
    reason: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Graph:
    """后端导入图。

    Attributes:
        projects: 工程清单。
        test_files: 工程名 → 测试文件（相对后端根）。
        test_deps: 测试文件 → 依赖集合（深度由选测模式决定：闭包或直接依赖）。
    """

    projects: tuple[Project, ...]
    test_files: dict[str, tuple[str, ...]]
    test_deps: dict[str, frozenset[str]] = field(default_factory=dict)


def _is_test_file(path: Path, prefix: str) -> bool:
    """是否为测试文件（按配置前缀识别 `test_*.py`）。

    Args:
        path: 文件路径。
        prefix: 测试文件名前缀。

    Returns:
        bool: 测试文件返回 True。
    """
    return path.suffix == ".py" and path.name.startswith(prefix)


def _iter_py(root: Path) -> tuple[Path, ...]:
    """列出目录下全部 `.py`（跳过缓存 / 虚拟环境 / node_modules）。

    Args:
        root: 起始目录。

    Returns:
        tuple[Path, ...]: 文件路径（按路径排序）。
    """
    if not root.is_dir():
        return ()
    return tuple(path for path in sorted(root.rglob("*.py")) if not any(part in _EXCLUDED_PARTS for part in path.parts))


def _backend_meta(cfg: Config) -> tuple[Path, Path, str, tuple[str, ...]]:
    """取后端元信息（绝对路径、相对根、测试前缀、分组）。

    Args:
        cfg: 仓配置。

    Returns:
        tuple[Path, Path, str, tuple[str, ...]]: （后端根, 仓库根, 测试前缀, 分组）。
    """
    if cfg.backend is None:
        raise RuntimeError("未配置 `[backend]`，无法推导后端选测")
    return cfg.root / cfg.backend.root, cfg.root, cfg.backend.test_prefix, cfg.backend.groups


def discover_projects(cfg: Config) -> list[Project]:
    """发现后端工程（`<后端根>/<分组>/<工程>/{src,tests}`，两者都在才算）。

    不维护工程白名单——目录到位即被发现（「新增工程必登记」护栏的前提）。

    Args:
        cfg: 仓配置。

    Returns:
        list[Project]: 工程清单（分组声明序、组内按名排序）。

    Raises:
        RuntimeError: 未配置后端。
    """
    backend, root, _, groups = _backend_meta(cfg)
    projects: list[Project] = []
    for group in groups:
        group_dir = backend / group
        if not group_dir.is_dir():
            continue
        for item in sorted(group_dir.iterdir()):
            if not (item.is_dir() and (item / "src").is_dir() and (item / "tests").is_dir()):
                continue
            packages = tuple(
                child.name
                for child in sorted((item / "src").iterdir())
                if child.is_dir() and (child / "__init__.py").is_file()
            )
            if not packages:
                continue
            projects.append(
                Project(
                    name=packages[0],
                    src=(item / "src").relative_to(root).as_posix(),
                    tests=(item / "tests").relative_to(backend).as_posix(),
                    packages=packages,
                )
            )
    return projects


def module_of_source(rel: str, project: Project) -> str:
    """源文件（相对仓库根）→ 模块名；非包内文件返回空串。

    Args:
        rel: 变更路径（相对仓库根）。
        project: 所属工程。

    Returns:
        str: 模块名（如 `a.b.c`）；不在包内则空串。
    """
    prefix = f"{project.src}/"
    if not rel.startswith(prefix) or not rel.endswith(".py"):
        return ""
    tail = rel[len(prefix) : -3].replace("/", ".")
    if tail.endswith(".__init__"):
        tail = tail[: -len(".__init__")]
    for package in project.packages:
        if tail == package or tail.startswith(f"{package}."):
            return tail
    return ""


def _resolve_relative(module: str, node: ast.ImportFrom) -> str:
    """把相对导入（`from .x import y`）解析为绝对模块名前缀。

    Args:
        module: 当前模块名。
        node: `ImportFrom` 节点。

    Returns:
        str: 绝对模块名前缀（无法解析时为空串）。
    """
    if node.level <= 0:
        return node.module or ""
    parts = module.split(".")
    keep = len(parts) - node.level
    if keep <= 0:
        return node.module or ""
    base = parts[:keep]
    if node.module:
        base = [*base, *node.module.split(".")]
    return ".".join(base)


def parse_imports(path: Path, module: str) -> set[str]:
    """解析单文件的绝对导入模块名集合（含 `from A import b` 的 `A.b` 形态）。

    Args:
        path: Python 文件路径。
        module: 该文件对应模块名（相对导入解析用；测试文件传空串）。

    Returns:
        set[str]: 导入的模块名集合。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(module, node)
            if not base:
                continue
            found.add(base)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{base}.{alias.name}")
    return found


def _transitive_deps(modules: dict[str, set[str]]) -> dict[str, set[str]]:
    """把「模块 → 直接导入」补成「模块 → 传递依赖闭包」（记忆化 DFS，环上截断）。

    Args:
        modules: 模块 → 直接导入集合。

    Returns:
        dict[str, set[str]]: 模块 → 传递依赖闭包。
    """
    resolved: dict[str, set[str]] = {}

    def walk(module: str, stack: set[str]) -> set[str]:
        cached = resolved.get(module)
        if cached is not None:
            return cached
        if module in stack:
            return set()
        stack.add(module)
        closure: set[str] = set()
        for dep in modules.get(module, set()):
            if dep in modules:
                closure.add(dep)
                closure.update(walk(dep, stack))
        stack.discard(module)
        resolved[module] = closure
        return closure

    for module in modules:
        walk(module, set())
    return resolved


def _conftest_direct(backend: Path, project: Project) -> dict[str, set[str]]:
    """收集工程测试目录内各 `conftest.py` 的直接导入（目录 → 导入集合）。

    Args:
        backend: 后端根。
        project: 工程。

    Returns:
        dict[str, set[str]]: 测试目录（相对测试根；根目录为 `.`）→ 直接导入模块集合。
    """
    found: dict[str, set[str]] = {}
    test_root = backend / project.tests
    for path in _iter_py(test_root):
        if path.name != "conftest.py":
            continue
        rel_dir = path.parent.relative_to(test_root).as_posix()
        found.setdefault(rel_dir, set()).update(parse_imports(path, ""))
    return found


def _conftest_scope(rel: str, project: Project, conftest: dict[str, set[str]]) -> set[str]:
    """取测试文件所在目录及其上级目录的 conftest 直接导入（不含测试根之上）。

    Args:
        rel: 测试文件（相对后端根）。
        project: 工程。
        conftest: 工程内 conftest 直接导入（`_conftest_direct` 结果）。

    Returns:
        set[str]: 覆盖该测试文件的 conftest 直接导入集合。
    """
    prefix = f"{project.tests}/"
    if not rel.startswith(prefix):
        return set()
    parts = rel[len(prefix) :].split("/")[:-1]
    dirs = ["."] + ["/".join(parts[: index + 1]) for index in range(len(parts))]
    found: set[str] = set()
    for item in dirs:
        found.update(conftest.get(item, set()))
    return found


def build_graph(cfg: Config, *, depth: str = "") -> Graph:
    """构建后端导入图（源模块依赖 + 测试依赖）。

    Args:
        cfg: 仓配置。
        depth: 依赖深度（`closure` 传递闭包 / `direct` 直接依赖；空串取配置）。

    Returns:
        Graph: 导入图。

    Raises:
        RuntimeError: 未配置后端，或未发现任何工程。
    """
    mode = depth or cfg.depth
    backend, root, test_prefix, _ = _backend_meta(cfg)
    projects = discover_projects(cfg)
    if not projects:
        raise RuntimeError(f"未发现任何后端工程（{backend}）")

    test_files: dict[str, tuple[str, ...]] = {}
    for project in projects:
        files: list[str] = []
        for path in _iter_py(backend / project.tests):
            if _is_test_file(path, test_prefix):
                files.append(path.relative_to(backend).as_posix())
        test_files[project.name] = tuple(files)

    modules: dict[str, set[str]] = {}
    for project in projects:
        for path in _iter_py(root / project.src):
            module = module_of_source(path.relative_to(root).as_posix(), project)
            if module:
                modules[module] = parse_imports(path, module)
    closures = _transitive_deps(modules) if mode == "closure" else {}

    conftest: dict[str, dict[str, set[str]]] = {}
    if mode == "direct":
        for project in projects:
            conftest[project.name] = _conftest_direct(backend, project)

    test_deps: dict[str, frozenset[str]] = {}
    for project in projects:
        for rel in test_files[project.name]:
            direct: set[str] = {dep for dep in parse_imports(backend / rel, "") if dep in modules}
            closure: set[str] = set(direct)
            if mode == "closure":
                for dep in direct:
                    closure.update(closures.get(dep, set()))
            else:
                closure.update(dep for dep in _conftest_scope(rel, project, conftest[project.name]) if dep in modules)
            test_deps[rel] = frozenset(closure)

    return Graph(projects=tuple(projects), test_files=test_files, test_deps=test_deps)


def _isomorphic_candidates(backend: Path, rel: str, project: Project, test_prefix: str) -> tuple[str, ...]:
    """目录同构候选：同名测试优先（`test_<原名>.py` / `test_<原名>_*.py`），无命中退回同构目录。

    Args:
        backend: 后端根。
        rel: 变更源文件（相对仓库根）。
        project: 所属工程。
        test_prefix: 测试文件名前缀。

    Returns:
        tuple[str, ...]: 候选测试文件（相对后端根）。
    """
    prefix = f"{project.src}/"
    if not rel.startswith(prefix) or not rel.endswith(".py"):
        return ()
    test_root = backend / project.tests
    stem = Path(rel).stem
    if stem != "__init__":
        exact = f"{test_prefix}{stem}.py"
        related = f"{test_prefix}{stem}_"
        same = [
            path.relative_to(backend).as_posix()
            for path in _iter_py(test_root)
            if _is_test_file(path, test_prefix) and (path.name == exact or path.name.startswith(related))
        ]
        if same:
            return tuple(sorted(same))
    parts = rel[len(prefix) : -3].split("/")
    sub = parts[1:-1]
    test_dir = test_root
    if sub:
        test_dir = test_root.joinpath(*sub)
    if not test_dir.is_dir():
        return ()
    pattern = "**/*.py" if sub else "*.py"
    found: list[str] = []
    for path in sorted(test_dir.glob(pattern)):
        if not _is_test_file(path, test_prefix) or "__pycache__" in path.parts:
            continue
        found.append(path.relative_to(backend).as_posix())
    return tuple(found)


def _extra_rule_for(rel: str, rules: tuple[ExtraTests, ...]) -> ExtraTests | None:
    """取命中的特殊路径规则。

    Args:
        rel: 变更路径（相对仓库根）。
        rules: 规则清单。

    Returns:
        ExtraTests | None: 命中的规则（未命中为 None）。
    """
    for rule in rules:
        if rel.startswith(rule.match_prefix):
            return rule
    return None


def is_global_change(rel: str, cfg: Config) -> bool:
    """变更是否属全局（影响全部后端工程）。

    Args:
        rel: 变更路径（相对仓库根）。
        cfg: 仓配置。

    Returns:
        bool: 全局变更返回 True。
    """
    rule = cfg.backend_global
    if rel in rule.exact or (rule.prefixes and rel.startswith(rule.prefixes)):
        return True
    return bool(rule.config_prefix) and rel.startswith(rule.config_prefix) and rel.endswith(rule.config_suffix)


def extra_tests(cfg: Config, changed: list[str]) -> tuple[list[str], list[str]]:
    """按特殊路径规则推导「必跑测试」与说明。

    Args:
        cfg: 仓配置。
        changed: 变更路径（相对仓库根）。

    Returns:
        tuple[list[str], list[str]]: （测试文件（相对后端根）, 说明）。
    """
    backend, _, test_prefix, _ = _backend_meta(cfg)
    picked: dict[str, None] = {}
    notes: list[str] = []
    for rel in changed:
        rule = _extra_rule_for(rel, cfg.backend_extra)
        if rule is None:
            continue
        added = 0
        if rule.test_dir_suffix:
            for path in _iter_py(backend):
                if not _is_test_file(path, test_prefix):
                    continue
                test_rel = path.relative_to(backend).as_posix()
                if rule.test_dir_suffix in f"/{test_rel}" and test_rel not in picked:
                    picked[test_rel] = None
                    added += 1
            if added:
                notes.append(f"{rule.match_prefix} 变更 ⇒ 并入同构测试 {added} 个")
        explicit = list(rule.test_files)
        if rule.mapping:
            mapping = dict(rule.mapping)
            name = rel[len(rule.match_prefix) :]
            target = mapping.get(name)
            if target is not None:
                explicit.append(target)
            elif rule.fallback_dir:
                explicit.extend(
                    path.relative_to(backend).as_posix()
                    for path in _iter_py(backend / rule.fallback_dir)
                    if _is_test_file(path, test_prefix)
                )
        count = 0
        for item in explicit:
            if item not in picked and (backend / item).is_file():
                picked[item] = None
                count += 1
        if count:
            notes.append(f"{rule.match_prefix} 变更 ⇒ 并入其护栏用例 {count} 个")
    return list(picked), notes


def all_test_paths(cfg: Config) -> list[str]:
    """全部后端测试文件（相对后端根，按工程序）。

    Args:
        cfg: 仓配置。

    Returns:
        list[str]: 测试文件清单。
    """
    graph = build_graph(cfg)
    paths: list[str] = []
    for project in graph.projects:
        paths.extend(graph.test_files[project.name])
    return paths


def tests_for_modules(cfg: Config, modules: list[str]) -> list[str]:
    """扩圈用：返回直接 / 传递引用给定模块的测试文件。

    Args:
        cfg: 仓配置。
        modules: 目标模块名。

    Returns:
        list[str]: 引用这些模块的测试文件（相对后端根，按工程序）。
    """
    graph = build_graph(cfg)
    wanted = set(modules)
    matched = {rel for rel, deps in graph.test_deps.items() if deps & wanted}
    ordered: list[str] = []
    for project in graph.projects:
        for rel in graph.test_files[project.name]:
            if rel in matched:
                ordered.append(rel)
    return ordered


def unregistered_projects(discovered: list[Project], cfg: Config) -> list[str]:
    """自动发现的工程里未登记到全量档范围 / 覆盖率范围的项。

    Args:
        discovered: 自动发现的工程。
        cfg: 仓配置。

    Returns:
        list[str]: 未登记项说明（空表示登记齐备）。
    """
    if cfg.backend is None:
        return []
    roots = set(cfg.backend.projects)
    packages = set(cfg.backend.coverage_packages)
    missing: list[str] = []
    for project in discovered:
        directory = project.src.removeprefix(f"{cfg.backend.root}/").removesuffix("/src")
        if directory not in roots:
            missing.append(f"{directory}：未登记到全量档工程范围")
        if project.name not in packages:
            missing.append(f"{project.name}：未登记到覆盖率采集范围")
    return missing


def _module_of_changed(rel: str, projects: tuple[Project, ...], rules: tuple[ExtraTests, ...]) -> list[str]:
    """变更路径对应的包内模块（特殊路径规则声明不计覆盖率的除外）。

    Args:
        rel: 变更路径（相对仓库根）。
        projects: 工程清单。
        rules: 特殊路径规则。

    Returns:
        list[str]: 模块名清单。
    """
    rule = _extra_rule_for(rel, rules)
    if rule is not None and not rule.coverage:
        return []
    return [module for module in (module_of_source(rel, project) for project in projects) if module]


def build_selection(
    cfg: Config,
    changed: list[str],
    *,
    base_threshold: int | None = None,
    base_ratio: float | None = None,
    force_full: bool = False,
    depth: str = "",
) -> Selection:
    """按变更路径推导后端选测结果。

    Args:
        cfg: 仓配置。
        changed: 变更路径（相对仓库根）。
        base_threshold: 基础模块绝对阈值（缺省取配置）。
        base_ratio: 基础模块占比阈值（缺省取配置）。
        force_full: 强制退化全量。
        depth: 依赖深度（`closure` 传递闭包 / `direct` 直接依赖；空串取配置）。

    Returns:
        Selection: 选测结果。

    Raises:
        RuntimeError: 未配置后端或未发现工程。
    """
    backend, _, test_prefix, _ = _backend_meta(cfg)
    mode = depth or cfg.depth
    threshold = cfg.base_threshold if base_threshold is None else base_threshold
    ratio_limit = cfg.base_ratio if base_ratio is None else base_ratio
    graph = build_graph(cfg, depth=mode)
    changed_set = {rel for rel in (gitutil.norm(item) for item in changed) if rel}
    backend_prefix = cfg.backend.root if cfg.backend is not None else ""

    global_change = force_full
    for rel in changed_set:
        if is_global_change(rel, cfg):
            global_change = True
    guard_tests, guard_notes = extra_tests(cfg, list(changed_set))

    reasons: list[str] = []
    notes: list[str] = []
    degraded = False
    if force_full:
        reasons.append("变更基准不可解析 ⇒ 强制退化全量")

    selected_by_project: dict[str, set[str]] = {}
    global_modules = (
        {
            module
            for rel in changed_set
            for module in (module_of_source(rel, project) for project in graph.projects)
            if module
        }
        if mode == "direct"
        else set()
    )
    for project in graph.projects:
        files = graph.test_files[project.name]
        total = len(files)
        modules = global_modules or {
            module for rel in changed_set for module in [module_of_source(rel, project)] if module
        }
        selected: set[str] = {rel for rel in files if f"{backend_prefix}/{rel}" in changed_set}
        if global_change:
            selected.update(files)
            if total:
                degraded = True
                if "全局配置 / 迁移 / 部署变更 ⇒ 全量" not in reasons:
                    reasons.append("全局配置 / 迁移 / 部署变更 ⇒ 全量")
        else:
            for rel in changed_set:
                selected.update(_isomorphic_candidates(backend, rel, project, test_prefix))
            if modules:
                for test_rel in files:
                    if test_rel in selected:
                        continue
                    if graph.test_deps[test_rel] & modules:
                        selected.add(test_rel)
            project_degraded = False
            if modules and not selected:
                if mode == "direct":
                    notes.append(f"{project.name}：变更模块无测试命中 ⇒ 本次跳过该工程（缺测交推送前预检）")
                else:
                    project_degraded = True
                    notes.append(f"{project.name}：变更模块无测试引用者 ⇒ 退化工程级全量")
            if mode != "direct" and (len(selected) > threshold or (total and len(selected) / total > ratio_limit)):
                project_degraded = True
                notes.append(
                    f"{project.name}：受影响测试 {len(selected)}/{total} 超过基础模块阈值"
                    f"（{threshold} 个或 {ratio_limit:.0%}）⇒ 退化工程级全量"
                )
            if project_degraded:
                selected.update(files)
                degraded = True
                reasons.append(f"{project.name} 退化工程级全量")
        selected_by_project[project.name] = selected

    guard_added = 0
    for test_rel in guard_tests:
        for project in graph.projects:
            if test_rel in graph.test_files[project.name] and test_rel not in selected_by_project[project.name]:
                selected_by_project[project.name].add(test_rel)
                guard_added += 1
                break
    if guard_added:
        notes.extend(guard_notes)

    test_paths: list[str] = []
    metrics: list[ProjectMetrics] = []
    for project in graph.projects:
        files = graph.test_files[project.name]
        selected = selected_by_project[project.name]
        ordered = [rel for rel in files if rel in selected]
        test_paths.extend(ordered)
        metrics.append(
            ProjectMetrics(
                name=project.name,
                picked=len(ordered),
                total=len(files),
                degraded=bool(files) and len(ordered) == len(files),
            )
        )

    cov_modules: set[str] = set()
    for rel in changed_set:
        cov_modules.update(_module_of_changed(rel, graph.projects, cfg.backend_extra))

    return Selection(
        test_paths=tuple(test_paths),
        cov_modules=tuple(sorted(cov_modules)),
        degraded=degraded,
        reason="；".join(reasons),
        picked=len(test_paths),
        total=sum(metric.total for metric in metrics),
        projects=tuple(metrics),
        notes=tuple(notes),
    )


def expand_from_coverage(coverage_path: Path, modules: tuple[str, ...] | list[str]) -> list[str]:
    """扩圈：读覆盖率报告，返回「有缺行」的目标模块。

    Args:
        coverage_path: `coverage.json` 路径。
        modules: 覆盖率目标模块（门禁范围）。

    Returns:
        list[str]: 存在未覆盖行的模块（空表示已达标或报告不可读）。
    """
    try:
        data = json.loads(coverage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    files = data.get("files") or {}
    missing: dict[str, None] = {}
    for module in modules:
        suffix = module.replace(".", "/")
        candidates = (f"{suffix}.py", f"{suffix}/__init__.py")
        for key, entry in files.items():
            normal = str(key).replace("\\", "/")
            if not any(normal.endswith(candidate) for candidate in candidates):
                continue
            if entry.get("missing_lines"):
                missing.setdefault(module, None)
            break
    return list(missing)


def discover_frontend_packages(cfg: Config) -> list[FrontendPackage]:
    """发现前端 pnpm 工程（`<前端根>/<分组>/*/package.json`）。

    Args:
        cfg: 仓配置。

    Returns:
        list[FrontendPackage]: 工程清单（分组声明序、组内按名排序）。
    """
    if cfg.frontend is None:
        return []
    frontend = cfg.root / cfg.frontend.root
    packages: list[FrontendPackage] = []
    for group in cfg.frontend.groups:
        group_dir = frontend / group
        if not group_dir.is_dir():
            continue
        for item in sorted(group_dir.iterdir()):
            manifest = item / "package.json"
            if not item.is_dir() or not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = str(data.get("name") or "")
            if not name:
                continue
            merged: dict[str, object] = {}
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                block = data.get(section)
                if isinstance(block, dict):
                    merged.update({str(key): value for key, value in block.items()})
            deps = tuple(
                dep
                for dep, spec in merged.items()
                if isinstance(spec, str) and spec.startswith(cfg.frontend.workspace_protocol)
            )
            packages.append(
                FrontendPackage(
                    name=name,
                    path=item.relative_to(cfg.root).as_posix(),
                    workspace_deps=deps,
                )
            )
    return packages


def build_frontend_selection(
    cfg: Config, changed: list[str], *, force_full: bool = False, depth: str = ""
) -> FrontendSelection:
    """按变更路径推导受影响前端包（依赖反向闭包 + 根级配置退化）。

    Args:
        cfg: 仓配置。
        changed: 变更路径（相对仓库根）。
        force_full: 强制退化为全部包。
        depth: 依赖深度（`closure` 根级配置退化为全部包 / `direct` 只取直接命中包；空串取配置）。

    Returns:
        FrontendSelection: 前端选测结果。
    """
    if cfg.frontend is None:
        return FrontendSelection()
    mode = depth or cfg.depth
    packages = discover_frontend_packages(cfg)
    names = [package.name for package in packages]
    frontend_prefix = f"{cfg.frontend.root}/"

    direct: set[str] = set()
    global_change = force_full
    notes: list[str] = []
    for item in changed:
        rel = gitutil.norm(item)
        if not rel:
            continue
        rule = cfg.frontend_global
        if rel in rule.exact or (rule.prefixes and rel.startswith(rule.prefixes)):
            global_change = True
            continue
        if not rel.startswith(frontend_prefix):
            continue
        matched = False
        for package in packages:
            if rel == package.path or rel.startswith(f"{package.path}/"):
                direct.add(package.name)
                matched = True
                break
        if not matched:
            global_change = True

    if global_change and mode != "direct":
        affected = set(names)
        reason = (
            "变更基准不可解析 ⇒ 强制退化全部前端包"
            if force_full
            else "根级配置 / 共享脚本 / 未归属文件变更 ⇒ 全部前端包"
        )
        notes.append(reason)
    else:
        affected = set(direct)
        grew = True
        while grew:
            grew = False
            for package in packages:
                if package.name in affected:
                    continue
                if any(dep in affected for dep in package.workspace_deps):
                    affected.add(package.name)
                    grew = True
        reason = ""
        if global_change:
            notes.append("根级配置 / 共享脚本 / 未归属文件变更 ⇒ 前端全量留推送前预检")
        if direct:
            notes.append(f"{'、'.join(sorted(direct))} 变更 ⇒ 依赖反向闭包 {len(affected)} 个包")

    return FrontendSelection(
        packages=tuple(name for name in names if name in affected),
        degraded=global_change and mode != "direct",
        reason=reason,
        notes=tuple(notes),
    )


def read_changed(
    root: Path,
    *,
    changed_from: str = "",
    changed_files: tuple[str, ...] = (),
    changed_file_list: str = "",
) -> list[str]:
    """汇总变更路径（显式文件 ∪ 清单文件 ∪ git diff）。

    Args:
        root: 仓库根。
        changed_from: git 基准。
        changed_files: 显式变更文件（可重复）。
        changed_file_list: 变更清单文件路径（一行一个）。

    Returns:
        list[str]: 去重后的变更路径（相对仓库根）。

    Raises:
        RuntimeError: `changed_from` 不可解析 —— 调用方应显式退化全量。
    """
    if changed_from and not gitutil.ref_exists(root, changed_from):
        raise RuntimeError(f"变更基准不可解析：{changed_from}")
    collected: dict[str, None] = {}
    for item in changed_files:
        rel = gitutil.norm(item)
        if rel:
            collected.setdefault(rel, None)
    if changed_file_list:
        path = Path(changed_file_list)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                rel = gitutil.norm(line)
                if rel:
                    collected.setdefault(rel, None)
    if changed_from:
        from_git, _ = gitutil.changes(root, changed_from)
        for rel in from_git:
            collected.setdefault(rel, None)
    return list(collected)
