# engkit · 跨仓工程化工具包

> 文档链接与口径护栏 · 预检与重命令包装 · 用例登记 · 一次实现、各仓按配置接入

各仓以 **仓根一份 `engkit.toml`** 接入，包内不含任何仓名与目录字面；新增仓库只加配置、不改代码。

## 子命令

| 子命令 | 用途 |
| --- | --- |
| `engkit links` | 文档相对链接与锚点校验（跳过外部链接、行内代码与本地凭据目录） |
| `engkit docs-scope` | 文档口径护栏：进度类字段只允许出现在阶段计划与总体规划 |
| `engkit selection` | 变更 → 受影响测试文件 / 覆盖率模块 / 前端包（目录同构 + AST 导入图双重收敛；`--depth direct` 只认直接依赖） |
| `engkit run-selected` | 定向执行器：选测 → 跑 pytest → 覆盖率门禁 → 扩圈 → 退化兜底 → 度量 |
| `engkit run-changed` | 开发环：改动文件 ruff → 后端直接依赖测试 → 前端受影响包（不带覆盖率、不退化全量） |
| `engkit coverage` | 覆盖率门禁：合并并行采集数据并判定（行 + 分支） |
| `engkit preflight` | 本地预检：档位（定向 / `--fast` / `--full`）、预算护栏、步骤调度、自测按需 |
| `engkit run-once` | 重命令包装：同命令同工作区状态下窗口内只真跑一次，命中即回放 |
| `engkit guard` | 重命令拦截：未包装跑重命令即报错；含 `.pth` 启动钩子安装 / 校验 / 卸载 |
| `engkit kiwi` | Kiwi TCMS 用例批量登记与回读（SSH + `manage.py shell`，凭据不入库） |

## 安装

```bash
uv sync                       # 本仓开发（含 pytest / ruff / pyright）
pip install -e .              # 或把本包装进某仓环境
```

各仓推荐以 **git 源依赖锁 rev** 引用（与既有共享基座同构）：

```toml
[tool.uv.sources]
engkit = { git = "<远端>/engkit.git", rev = "<SHA>" }
```

## 快速上手

```bash
engkit links                 # 文档链接与锚点
engkit docs-scope            # 文档口径
engkit preflight --fast      # 秒级自检
engkit run-once -- engkit preflight   # 分钟级命令经包装调用
```

配置字段全表与接入步骤见 `engkit文档/`；本仓自身门禁（基础门禁直跑 + 自举烟雾）见 `.gitlab-ci.yml`。
