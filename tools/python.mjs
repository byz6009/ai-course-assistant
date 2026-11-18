import { spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'

export function runPython(args) {
  const candidates = process.env.KEJI_PYTHON
    ? [[process.env.KEJI_PYTHON, []]]
    : process.platform === 'win32' ? [['py', ['-3']], ['python', []], ['python3', []]] : [['python3', []], ['python', []]]
  for (const [command, prefix] of candidates) {
    const probe = spawnSync(command, [...prefix, '-c', 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'], { windowsHide: true })
    if (probe.status !== 0) continue
    const result = spawnSync(command, [...prefix, ...args], { stdio: 'inherit', windowsHide: true })
    return result.status ?? 1
  }
  console.error('需要 Python 3.10+。请安装 Python，或设置 KEJI_PYTHON 为解释器绝对路径。')
  return 1
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = runPython(process.argv.slice(2))
}
