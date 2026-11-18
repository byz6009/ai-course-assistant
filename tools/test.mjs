import { spawnSync } from 'node:child_process'
import { runPython } from './python.mjs'

const pythonStatus = runPython(['-m', 'unittest', 'discover', '-s', 'tests', '-v'])
const jsStatus = spawnSync(process.execPath, ['--test', 'tests/blackboard-parser.test.mjs'], { stdio: 'inherit', windowsHide: true }).status ?? 1
process.exitCode = pythonStatus || jsStatus
