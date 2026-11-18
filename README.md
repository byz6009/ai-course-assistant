# 课迹

面向大学课程的本机学习辅助工具。第三阶段完成看板、资料收集、回放字幕整理的闭环：导入 SRT/VTT → 后台整理 → 核对原文与时间 → 保存摘要版本 → 确认学习任务 → 看板勾选。默认使用真实字幕的本地原文摘录；AI 摘要接口可配置，未配置时明确提示。

本次开发基于尚未合并的第二阶段 PR #2（`71e1e6d`）。本地分支 `feature/stage3-replay`；Git 历史及评审修复见 [阶段交接](docs/stage3/README.md)。作业辅导、备考仍是第一阶段原型，不计作真实生成能力。

## 运行可运行版

将 `课迹第三阶段可运行版.zip` 解压到独立目录，需要 Python 3.10+，无需安装前端依赖。Windows 双击 `start-keji.cmd`；脚本优先查找 Python，也可使用本机 Codex 随附的 Python。

也可在解压目录执行：

```sh
python server/app.py
```

打开 <http://127.0.0.1:8765>。服务只监听本机，数据保存到 `.local-data/courses.sqlite3`。这不是公网多人版本。Windows 启动日志在 `.runtime/`；前台命令用 Ctrl+C 退出。后台启动器记录 `.runtime/server.pid`，可在任务管理器中核对该进程后结束。

可选导入明确标注的合成示例（不会代替用户确认学习任务）：

```sh
python tools/seed_demo.py
```

## 从源码构建

Node 22.12+、Python 3.10+。后端没有第三方 Python 依赖。

```sh
npm ci
npm test
npm run build
npm start
```

`npm` 脚本会探测 `py -3`、`python`、`python3`；解释器不在 PATH 时，设置 `KEJI_PYTHON` 为绝对路径。开发时分别运行 `npm run api` 与 `npm run dev`，打开 <http://127.0.0.1:5173>；API 默认端口 8765，变更时同时修改 Vite 代理。

## 使用

1. 资料收集：通过现有教学网 Chrome 导入助手、手动表单或 JSON 导入课程资料、课表和截止。来源与版本保留；附件仍只保存目录和链接。
2. 回放整理：选择课程或新建课程，上传 UTF-8 SRT/VTT（最多 5 MiB）；先校验、预览，再确认保存。原始回放链接和总时长可选。
3. 点击“生成摘要”。本地摘录按章节前中后段选取代表性原文，不调用模型；不等同于语义总结。处理过程中可离开页面。
4. 点击时间引用定位字幕；可编辑摘要并保存新版本，查看旧版本或导出包含原文引用的 JSON。
5. 选择知识点，点击“确认加入学习任务”。每讲最多一条任务，保留确认版本的来源快照；在看板勾选后刷新仍保留。

没有字幕的音视频需先在外部转写。本阶段没有音视频自动转写、OCR、公开网络检索、真实作业辅导或备考生成。

## 模型服务配置

打开侧栏 **AI 服务设置**（`/settings/ai`），输入服务商的 API 地址、模型 ID 和 API Key。可选读取模型列表，再点击“测试连接与摘要”，通过后“保存并启用”。返回回放页，将整理方式切换为“AI 摘要”即可使用。配置立即生效，不需要重启。

支持 OpenAI 兼容 `POST /chat/completions`，接受 Base URL 或完整接口地址；支持 JSON 模式开关和 5–90 秒生成超时。测试仅发送合成字幕，最多等待 20 秒，不保存配置、不创建课程或学习任务。密钥由后端保存在被忽略的 `.local-data/ai-config.json`（未加密），不会回显或放入浏览器存储；不要分享该文件。更换地址需重新输入密钥，停用会清除本机保存的密钥。

**完整步骤、地址示例、字幕工作流、故障排查、数据备份见 [AI 使用教程](docs/stage3/AI使用教程.md)。与上一阶段汇报和推进规划的对照见 [阶段衔接核查](docs/stage3/stage2-alignment.md)。**

已有环境配置仍可沿用：在**后端进程环境**设置下面三个变量后启动；页面配置优先，可在设置页切回环境变量。`.env.example` 仅作为说明，不会被自动读取。不要将密钥写进 `VITE_` 变量或源码。

```powershell
$env:KEJI_AI_BASE_URL = 'https://your-provider.example/v1'
$env:KEJI_AI_MODEL = 'your-model-name'
# KEJI_AI_API_KEY 由本机环境或密钥管理工具注入，不写入项目文件。
npm start
```

全部配置齐全后，回放页显示“AI 摘要”选项。超时上限 90 秒；不自动重试、不会静默降级。模型输入 JSON 最多 60000 字符，超限需拆分课次或使用本地摘录。模型仅选择字幕 ID，后端生成引用时间与原文。引用可定位不等于语义正确，真实模型质量仍需组内核对。

## 验证与备份

```sh
npm test
npm run build
npx playwright install chromium
npm run test:e2e
python tools/backup.py
```

已安装 Edge 时可设置 `KEJI_BROWSER_CHANNEL=msedge` 后直接执行端到端测试，不需下载 Chromium。测试使用 8766 端口及 `.runtime/` 内独立数据库，报告与截图写入 `test-results/`。备份通过 SQLite Backup API 完成，可在服务运行时执行；恢复时停止服务，保留原库，将备份复制为数据库路径后重启。

## 代码位置

|路径|作用|
|---|---|
|`server/subtitles.py`|SRT/VTT 校验、时间解析、内容稳定 ID|
|`server/summarizers.py`|本地摘录及模型适配器、引用校验|
|`server/ai_settings.py`、`src/views/AISettingsView.vue`|模型配置、密钥保留/清除、连接测试与使用教程|
|`server/replays.py`|持久化任务队列、版本、确认学习任务|
|`server/app.py`|本机 API、来源校验和静态页面|
|`src/views/ReplayView.vue`、`src/components/ReplayImport.vue`|回放正式页面、字幕导入|
|`src/views/DashboardView.vue`、`MaterialsView.vue`|课表、截止、学习任务、课程资料|
|`browser-extension/`|第二阶段教学网采集助手|
|`tests/`|Python、DOM 回归与浏览器测试|
|`docs/stage3/`|接口、进度、AI 开发过程、验收与演示步骤|

原型保留在 `/demo/dashboard`、`/demo/materials`、`/demo/replay`；`/test/hello` 可直接访问或刷新。原型重置仅清理原型浏览器状态，不删除正式数据库。
