/**
 * Vitest 全量测试守卫（跨仓唯一实现）。
 *
 * 用法：各前端包的 `vitest.config.ts` 在 `test.globalSetup` 指向本文件（仓内薄包装转发）。
 *
 * 口径：
 * - 开发期间（本地非 CI）不允许「无过滤全跑一个包」——一次跑多个包自然更不允许；
 * - 放行：CI、推送入口 / 预检的内部授权标记、开发环已选包标记、显式测试文件 / 过滤参数；
 * - 与后端 pytest 守卫同口径：全量测试只由 CI 执行，开发环只跑受影响包。
 */

/** 开发环注入：本次运行已由变更选测选中（值为包名，仅作可读标记）。 */
export const SELECTED_ENV = 'ENGKIT_VITEST_SELECTED'

/** 推送入口 / 预检的内部授权标记（与后端 pytest 守卫同名同义）。 */
export const FULL_AUTH_ENV = 'ENGKIT_FULL_TESTS_AUTHORIZED'

/** CI 环境标识（任一存在即视为 CI，放行）。 */
export const CI_ENVS = ['CI', 'GITLAB_CI', 'GITHUB_ACTIONS', 'JENKINS_URL', 'BUILDKITE', 'TF_BUILD']

/** vitest 子命令（不算测试文件过滤）。 */
const SUBCOMMANDS = new Set(['run', 'watch', 'dev', 'bench', 'related', 'list'])

/** 带取值的选项（其取值不算位置参数）。 */
const VALUE_OPTIONS = new Set([
  '--project',
  '--dir',
  '--root',
  '--config',
  '--reporter',
  '--outputFile',
  '--coverage.reportsDirectory',
  '--coverage.include',
  '--coverage.exclude',
  '--coverage.thresholds.lines',
  '--coverage.thresholds.branches',
  '--coverage.thresholds.functions',
  '--coverage.thresholds.statements',
  '--testNamePattern',
  '-t',
])

/** 本身即表示「已收窄范围」的选项（无需位置参数）。 */
const NARROW_OPTIONS = new Set(['--changed', '--related', '--shard'])

/**
 * 环境变量是否为真值。
 *
 * @param {string} name 变量名。
 * @returns {boolean} `1` / `true` / `yes` / `on` 返回 true。
 */
function truthy(name) {
  return ['1', 'true', 'yes', 'on'].includes(String(process.env[name] || '').trim().toLowerCase())
}

/**
 * 是否已获授权跑全量 / 已处于选定范围。
 *
 * @returns {boolean} 放行返回 true。
 */
export function isAllowed() {
  const selected = String(process.env[SELECTED_ENV] || '').trim()
  return CI_ENVS.some((name) => Boolean(process.env[name])) || Boolean(selected) || truthy(FULL_AUTH_ENV)
}

/**
 * 命令行是否已显式收窄测试范围。
 *
 * @param {string[]} argv 命令行参数（不含程序名）。
 * @returns {boolean} 已收窄返回 true。
 */
export function hasExplicitScope(argv) {
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg.startsWith('-')) {
      if (VALUE_OPTIONS.has(arg)) {
        index += 1
        continue
      }
      if (NARROW_OPTIONS.has(arg)) {
        return true
      }
      continue
    }
    if (SUBCOMMANDS.has(arg)) {
      continue
    }
    return true
  }
  return false
}

/**
 * 全量测试拦截提示。
 *
 * @returns {string} 提示文本。
 */
export function guidance() {
  return [
    '[全量测试守卫] 拒绝执行：开发期间不允许跑全量前端测试（本地非 CI）。',
    '  开发中改完即测请用开发环（按变更自动推导受影响包）：',
    '    engkit run-changed',
    '  调试单个 / 少量用例时显式带过滤：',
    '    pnpm --filter <包> test tests/foo.spec.ts',
    '  全量测试只由 CI 执行；口径见《测试规范》「变更影响分析」与《AI开发规范》「覆盖率要求」。',
  ].join('\n')
}

/**
 * Vitest globalSetup 入口：命中全量即抛错，用例零执行。
 *
 * @throws {Error} 开发期间无过滤全跑时抛出。
 */
export default function setup() {
  const argv = process.argv.slice(2)
  if (isAllowed() || hasExplicitScope(argv)) {
    return
  }
  throw new Error(guidance())
}
