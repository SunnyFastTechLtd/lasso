import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 图标完整性检查（构建期执行）
 *
 * `Icon` 组件按名字查注册表，名字写错时运行期只回退成一个占位圆点。
 * 这里在 `tsc` 之前静态扫描源码里所有图标引用，只要出现未注册的名字就让构建失败。
 */

const srcRoot = fileURLToPath(new URL('../src', import.meta.url))
const iconsFile = path.join(srcRoot, 'components/ui/icons.ts')

function walk(dir) {
  const files = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) files.push(...walk(full))
    else if (/\.(ts|tsx)$/.test(entry.name)) files.push(full)
  }
  return files
}

function registeredNames() {
  const source = fs.readFileSync(iconsFile, 'utf8')
  const marker = 'ICON_MAP: Record<string, LucideIcon> = {'
  const start = source.indexOf(marker)
  if (start < 0) throw new Error('icons.ts 中找不到 ICON_MAP 定义')
  const body = source.slice(source.indexOf('{', start), source.lastIndexOf('}'))
  return new Set(body.match(/[A-Za-z0-9]+/g) ?? [])
}

// 只匹配真正的图标引用：<Icon name="X">、icon="X"、icon={'X'}、icon: 'X'
const USAGE_PATTERNS = [
  /<Icon\b[^>]*?\bname=\{?['"]([A-Za-z0-9]+)['"]\}?/g,
  /\bicon=\{?['"]([A-Za-z0-9]+)['"]\}?/g,
  /\bicon:\s*['"]([A-Za-z0-9]+)['"]/g,
]

const registered = registeredNames()
const missing = new Map()

for (const file of walk(srcRoot)) {
  if (file === iconsFile) continue
  const source = fs.readFileSync(file, 'utf8')
  for (const pattern of USAGE_PATTERNS) {
    for (const match of source.matchAll(pattern)) {
      const name = match[1]
      if (registered.has(name)) continue
      if (!missing.has(name)) missing.set(name, new Set())
      missing.get(name).add(path.relative(srcRoot, file).replaceAll('\\', '/'))
    }
  }
}

if (missing.size > 0) {
  console.error(`[check-icons] 发现 ${missing.size} 个未注册的图标名：`)
  for (const [name, files] of [...missing].sort(([a], [b]) => a.localeCompare(b))) {
    console.error(`  - ${name}  ←  ${[...files].join(', ')}`)
  }
  console.error(`[check-icons] 请在 src/components/ui/icons.ts 的 ICON_MAP 中登记后再构建。`)
  process.exit(1)
}

console.log(`[check-icons] 通过：${registered.size} 个已注册图标，全部引用均可解析。`)
