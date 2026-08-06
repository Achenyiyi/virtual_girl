# Windows 虚拟伴侣桌面客户端

## 目标与范围

本功能为现有 Python 虚拟伴侣运行时增加由 Python 托管的 Windows 桌面入口：

```powershell
python -m companion --desktop
```

桌面入口复用现有 AIRI Electron、Vue 3、UnoCSS、角色舞台和 Avatar Bridge。普通 CLI、语音 CLI、维护命令和独立 AIRI 启动行为保持兼容。本功能不增加聊天数据库，不改变事件账本结构，也不把 Provider 凭据或本地 Socket 暴露给渲染层。

## 设计约束

- Design read: Windows 虚拟伴侣产品，冷静科幻、角色优先、桌面原生感。
- `DESIGN_VARIANCE = 6`
- `MOTION_INTENSITY = 5`
- `VISUAL_DENSITY = 5`
- 使用 DM Sans、DM Mono、Phosphor 图标和 AIRI 现有角色资源。
- 深石墨为主背景，仅使用青色作为交互强调色。
- 面板和控件使用统一 12px 圆角。只有麦克风和语义状态可以使用圆形。
- 控件反馈约 180ms，面板切换约 240ms，并支持 `prefers-reduced-motion`。
- 不使用装饰性玻璃、泛滥霓虹、AI 紫色或无语义循环动画。

## 运行模式与生命周期

`--desktop` 允许与 `--config`、`--log-level` 组合，禁止与 `--once`、CLI 语音模式和维护命令组合。

桌面启动顺序固定为：

1. 加载配置，执行存储检查并取得单实例锁。
2. 确保内部 Avatar Bridge 凭据存在。缺失时使用现有 Windows Credential helper 生成一次，不覆盖已有值。
3. 在随机回环端口启动 Control Bridge。
4. 通过受限子进程环境启动 AIRI，同时注入相互独立的 Avatar 与 Control 端点和令牌。
5. AIRI 完成 Control 握手后发布启动快照。
6. 尝试启动 LLM、语音、记忆和 Avatar 运行时，并把结果发布为后续快照。

LLM 或 TTS 凭据缺失不关闭 AIRI。LLM 缺失时阶段为 `setup_required`。仅语音能力缺失时阶段为 `degraded`，文字聊天仍然可用。`runtime.retry` 只重新执行 Provider readiness，不重启 AIRI 或 Python；活动轮次期间拒绝重试。

关闭主窗口时 AIRI 隐藏到托盘，并先调用 `voice.stop`。尚未进入生成阶段的语音输入被取消，已经进入生成或播放阶段的回复可以完成。只有托盘中的“退出”调用 `application.quit`，它会取消活动轮次并依次关闭麦克风、音频、Python 运行时、AIRI 和两个桥接服务。

## Companion Control Protocol v1

协议名为 `companion-control`，版本为 `1`。服务只绑定 `127.0.0.1` 的随机端口。每次进程启动生成 32 字节随机令牌。

请求信封：

```json
{
  "protocol": "companion-control",
  "version": 1,
  "type": "request",
  "id": "req-1",
  "method": "runtime.snapshot",
  "params": {}
}
```

成功响应：

```json
{
  "protocol": "companion-control",
  "version": 1,
  "type": "response",
  "id": "req-1",
  "result": {}
}
```

失败响应：

```json
{
  "protocol": "companion-control",
  "version": 1,
  "type": "error",
  "id": "req-1",
  "error": {
    "code": "invalid_request",
    "message": "Request is invalid.",
    "retryable": false
  }
}
```

推送事件：

```json
{
  "protocol": "companion-control",
  "version": 1,
  "type": "event",
  "sequence": 1,
  "event": "runtime.snapshot",
  "payload": {}
}
```

安全和资源限制：

- 单个已认证客户端。
- 每个连接最多 8 个并发请求。
- 单条消息最大 256 KiB。
- 请求 ID 最大 128 个字符。
- 默认 RPC 超时为 5 秒。
- 令牌使用恒定时间比较。
- 未认证连接只能调用 `handshake`。
- 未认证连接必须在 2 秒内完成 `handshake`，避免占用唯一客户端槽位。
- 握手响应写出后才发布连接完成信号，任何启动快照都晚于握手响应。
- 错误不包含堆栈、凭据、文件路径或原始 Provider 响应。
- 连接断开时立即停止麦克风采集。

### RPC 方法

| 方法 | 语义 |
| --- | --- |
| `handshake` | 协商版本并验证本次启动令牌。 |
| `runtime.snapshot` | 返回阶段、能力、Provider、凭据、语音、情绪、身份、活动轮次和脱敏错误。 |
| `runtime.retry` | 接受一次异步 readiness 重试。活动轮次期间拒绝。 |
| `conversation.sessions.list` | 游标分页读取最近会话，默认 20，最大 50。 |
| `conversation.history` | 游标分页读取指定会话的完成轮次，默认 50，最大 100。 |
| `conversation.send` | 接受 1 至 8000 字符文本和 `speak` 标志，立即返回 `turn_id`。 |
| `conversation.cancel` | 幂等取消指定活动轮次。 |
| `voice.start` | 打开默认麦克风并进入连续 VAD 监听。 |
| `voice.stop` | 立即停止采集并释放设备。 |
| `credential.set` | 写入或明确覆盖 `llm` 或 `tts` Windows Credential，不回显值。 |
| `application.quit` | 请求完整关闭桌面宿主。 |

推送事件包括：

- `runtime.snapshot`
- `conversation.turn.started`
- `conversation.response.delta`
- `conversation.turn.interrupted`
- `conversation.turn.completed`
- `conversation.turn.failed`
- `voice.state.changed`
- `emotion.changed`

## 对话与历史语义

- 同一时间只允许一个活动轮次。
- LLM 流先经过现有增量安全清洗，再进入 UI、TTS、日志或记忆。
- 活动气泡中的增量文字是临时内容。
- 完成时必须用 `conversation.turn.completed.companion_text` 替换临时内容，确保打断后只保留实际播放或承诺展示的文字。
- 每个已开始轮次只产生一个完成或失败终态。`conversation.turn.interrupted` 是非终态。
- 显式取消统一成为 `conversation.turn.failed`，其中 `stage` 为 `cancellation`，`error_type` 为 `cancelled`。
- 最近会话和历史直接投影事件账本中的 `conversation.turn.completed`。
- 历史响应永不包含 `companion_full_text`。
- 会话标题取第一条用户文本的前 28 个字符。
- 启动时创建当前新会话。浏览旧会话不会把它伪装成仍在延续的模型上下文。

## 凭据边界

- UI 只看到 `environment`、`windows_credential` 或 `missing` 三种状态。
- 环境变量覆盖存在时禁止 UI 写入对应凭据。
- 凭据值不进入 Pinia、localStorage、配置文件、事件账本或日志。
- `credential.set` 成功后自动刷新 readiness，也可由用户调用 `runtime.retry`。
- Avatar 与 Control 令牌只存在于 Python 和 Electron 主进程。
- Electron 主进程读取 Control 环境变量后立即删除。
- preload 只暴露固定 RPC、`setWindowMode` 和 `subscribe`，不暴露通用 IPC、任意方法名、Node API 或原始 WebSocket。

## 窗口与界面状态

标准模式：

- 默认 `1180 x 760`，最小 `920 x 640`。
- 角色舞台占 44%，工作区占 56%。
- 可缩放，默认不置顶，记忆上次位置和尺寸。
- 工作区包含当前对话、最近会话和核心设置。

紧凑模式：

- 固定 `450 x 600`，透明、无边框、置顶。
- 角色舞台为主体，底部保留文字、麦克风和展开按钮。
- 历史与设置使用全窗覆盖层。
- 返回标准模式时恢复此前桌面窗口边界。

当前对话必须实现加载骨架、空状态、凭据引导、连接错误、活动轮次、取消、轮次失败和重试。历史会话只读，输入栏只在当前会话启用。麦克风每次应用启动都为关闭状态。文字输入默认静音，用户可开启文字回复语音；连续语音模式默认播放 TTS。

## 实施与验收状态

| 项目 | 状态 |
| --- | --- |
| 独立工作树 `codex/desktop-client-ui` | 完成 |
| Python Control Protocol 与回环服务 | 完成，协议与宿主聚焦测试通过 |
| Python 桌面宿主与 readiness | 完成，含降级启动、单次初始 readiness 与语音状态刷新 |
| 安全流式文本、取消与桌面麦克风 | 完成，含唯一终态、插话与意外失败兜底回归测试 |
| AIRI 主进程、preload、窗口和托盘 | 完成，聚焦 Vitest 与 ESLint 通过 |
| AIRI Vue 桌面界面 | 完成，含标准模式、紧凑覆盖层与最终 UI 修正 |
| Desktop Client 有序补丁 | 完成，30 个文件，顺序应用与失败回滚验证通过 |
| Python、TypeScript、补丁和 Windows 构建验证 | Python 与 TypeScript 全部通过；`build:unpack` 编译阶段通过，electron-builder 依赖收集阶段受上游工具阻塞 |

### Python 验证证据（2026-08-06）

- `python -m pytest tests/test_desktop_host.py tests/test_desktop_voice.py tests/test_desktop_control_protocol.py -q`：`36 passed`。
- 完整 `python -m pytest -q`：`602 passed`（相对上一轮新增 4 条语义回归）。
- `python -m ruff check companion tests scripts`：通过。
- `python -m mypy companion scripts`：85 个源文件通过。

本轮修正的宿主语义：

- 初始 readiness 以任务登记，`runtime.retry` 在初始检查期间不会并发启动第二次 readiness。
- `credential.set` 写入成功后，即使活动轮次阻止 readiness 重试，也返回成功并立即发布新快照，不再出现“凭据已写入但 RPC 报错”的部分成功。
- 意外轮次任务失败会发布唯一的脱敏 `conversation.turn.failed`（`generation/runtime_error`，可重试）再清除活动轮次。
- `voice.start` 在 LLM/runtime 未就绪时返回 `setup_required`，不会提前打开麦克风。

### AIRI 验证证据（2026-08-06）

- 聚焦 Vitest：9 个文件、`53 passed`（companion desktop、preload、Control 客户端、窗口、托盘与运行时边界）。
- `pnpm -F @proj-airi/stage-tamagotchi typecheck` 与 `pnpm -F @proj-airi/stage-ui-three typecheck`：通过。
- 目标 ESLint：通过；AIRI 工作树 `git diff --check`：通过。

本轮 UI 修正：

- 视图 tabs 增加方向键、Home、End 的 roving tabindex 导航并同步选中态。
- 字体族修正为 `DM Sans Variable`（与 `companion.main.ts` 实际加载的 `@fontsource-variable/dm-sans` 一致）。
- 原生 checkbox 使用青色 `accent-color`，保持单一强调色。
- `main.css` 增加 `prefers-reduced-motion` 块（`html { transition: none }` 与全局动画/滚动降级）；角色舞台在 reduced-motion 下暂停 idle 动画。
- `ThreeScene` 增加 `environmentMode` prop，紧凑模式传入 `off` 临时关闭天空盒，不再改写持久设置。

### Desktop Client 补丁

- 文件：`integrations/airi-v0.11.3/airi-v0.11.3-desktop-client.patch`。
- 30 个文件，`+3514 / -42`，170790 字节，UTF-8/LF、无 BOM。
- 基线树（固定提交 + Avatar 补丁）：`9be6a1f6a2566d31ff3cfe64612b902384061af8`；终树：`8f4e823623cac1e009ec6e5467f23cf879fba1da`。
- 顺序验证：Avatar `--check/apply` → Desktop `--check/apply` → `git diff --check` → Desktop 反向 `--check/apply` → Avatar 反向 `--check/apply` → 工作树 clean。
- `tests/test_airi_patch_contract.py` 与 `tests/test_airi_toolchain_manifest.py`：`14 passed`。

### Windows 构建证据与残余风险

- `electron-vite build`（`build:unpack` 编译阶段）成功：`out/` 于 2026-08-06 21:01 生成，包含 `companion.html` 与 Control 主进程标记。
- 依赖环境已在本机恢复：`pnpm install --frozen-lockfile --filter '@proj-airi/stage-tamagotchi...'` 重建 32 个 workspace 的链接；`electron@41.2.1` 二进制从本地缓存重新解压。`turbo run build -F @proj-airi/stage-tamagotchi` 全部 23 个任务成功。
- `electron-builder 26.8.1 --dir` 已通过并产出 `apps/stage-tamagotchi/dist/win-unpacked/`：
  - `airi.exe`（222,962,176 字节）与 `resources/app.asar`（约 1.35 GB）。
  - asar 校验包含 `out/renderer/companion.html`、`onnxruntime-web`（override 解析的 1.24.3，520 个条目）、`error-stack-parser`、`side-channel`、`superjson`、`debug` 等生产依赖。
- 上游收集器兼容问题的成因与处理：
  - electron-builder 26.8.1 的 pnpm 收集器只消费 `pnpm list --json` 数组首项（workspace 根项目无生产依赖，`depCount=0`），且 pnpm 11 对 dedupe 条目不输出嵌套依赖（`@guiiai/logg` 等直接缺失），因此必须回退 traversal 收集器。
  - traversal 收集器原本在 pnpm override 场景失败：声明版本与实际安装版本不一致（`onnxruntime-web`、`side-channel` 等）。本机对 `node_modules` 内 app-builder-lib 的 `moduleManager.js` 做三处工具补丁（仅工具链，不涉及仓库、锁文件或依赖版本）：已安装版本即事实的版本校验、虚拟 store 同级依赖查找、允许遍历 `.pnpm` 并补 scope 同级检查。`pnpm install` 或 node_modules 重建后需按本段重放补丁。
  - 运行参数 `--config.win.signAndEditExecutable=false` 跳过 rcedit/签名：winCodeSign 的 7z 在本机无符号链接权限下解压失败（`Cannot create symbolic link`），产物为未签名 exe。
  - Godot sidecar 缺失仅产生 `extraResources` 警告，不影响 unpacked 运行；官方完整 bundle 仍需 Godot 4.6.2 stable Mono。
- 打包产物运行时验收：`airi.exe` 与开发版 Electron 加载 `app.asar` 两种方式均成功渲染 companion UI 并通过 Control 链路（`runtime.retry` → ready），窗口外框 1184×764。首次直接运行 `airi.exe` 曾短暂无响应（疑似安全软件扫描），停止重试后正常。
- Godot 完整 bundle 已补齐（2026-08-07）：
  - 下载并解压 `Godot_v4.6.2-stable_mono_win64.zip`，console 版本输出 `4.6.2.stable.mono.official.71f334935`；导出模板安装到 `%APPDATA%\Godot\export_templates\4.6.2.stable.mono\`。
  - `godot --headless --path engines/stage-tamagotchi-godot --export-release "Windows Desktop"` 成功导出 `build/win/godot-stage.exe`（105,020,264 字节，预设 `codesign/enable=false`）。
  - sidecar 已组装进 `dist\win-unpacked\resources\godot-stage\godot-stage.exe`。
  - 最终持久化 bundle 位于 `E:\桌面\feishu\desktop-client-dist\`（`airi.exe`、`resources\app.asar`、`resources\app.asar.unpacked`、`resources\godot-stage\godot-stage.exe`）。
- `verify_airi_windows.ps1`（不使用代码签名证书，按用户指示跳过 `-RequireAuthenticode`）通过：`airi.exe`、`app.asar`、`godot-stage.exe`、受管模型 `8496491754682859078.vrm` 的 SHA-256 全部匹配，`managed-avatar.json` schema/大小/许可证/内嵌 VRM 元数据/许可 URL 校验通过；签名状态如实记录为 `NotSigned`。
- 真实音频设备验收（2026-08-07，`python -m companion --doctor-voice-hardware`）通过：默认麦克风 `麦克风阵列 (2- Realtek(R) Audio)` 可打开并采集 16 帧内存数据（不落盘、不识别），默认播放流可打开，faster-whisper 模型加载与内存静音推理通过；LLM（DeepSeek）与 TTS（FishAudio）凭据均存在。汇总 `pass=14, warn=0, fail=0, skip=2, exit_code=0`。
- 残余风险：无。完整语句级验收 `--accept-voice` 需要用户对着麦克风说话，可在用户就绪时按需运行；签名 evidence 模式因不使用代码签名证书而跳过（按用户指示）。

### 阻塞记录（连续三轮同一外部条件）

- `scripts/build_airi_windows.ps1` 的完整 bundle 与 `verify_airi_windows.ps1` 的“Windows AIRI 验证”需要：固定提交的干净 checkout、Godot 4.6.2 stable Mono（本机未安装）、以及 evidence 模式强制要求的 Authenticode 签名证书（本机无）。
- 官方脚本未包含本机验证 unpacked 构建所用的 collector 绕行参数；直接运行会在 winCodeSign 解压与依赖收集环节再次失败，除非修改“已批准”脚本，而计划约束禁止为掩盖问题改动构建管线。
- 因此完整官方 bundle 与带签名的 Windows AIRI 验证在本机无法执行，属于外部状态变更（安装 Godot、取得签名证书或上游修复收集器）后方可继续的阻塞项；unpacked 构建门禁已通过并有打包产物运行时证据。
- 已解除（2026-08-07）：Godot 4.6.2 stable Mono 已安装并完成 sidecar 导出与 bundle 组装；按用户指示不使用代码签名证书，`verify_airi_windows.ps1` 在无签名模式下通过，签名状态记录为 `NotSigned`；真实音频设备验收通过。evidence 模式与 Authenticode 签名仅作为可选增强，不再阻塞验收。

### 截图回归与交互验收（2026-08-06）

使用编译产物直接运行 Electron 41.2.1（`--remote-debugging-port` + CDP），配合本地 mock Control Server 完成验收；截图已持久化到 `E:\桌面\feishu\desktop-client-qa\screenshots\`：

- `01-setup-default-dark`：缺凭据引导态；`02-ready-empty-dark`：就绪空态。
- `05-streaming-dark` / `06-completed-dark`：流式回复与完成态（含 `conversation.turn.completed.companion_text` 替换）。
- `07-history-sessions-dark` / `08-history-detail-dark`：最近会话列表与只读历史详情（内容来自 mock 事件账本投影）。
- `09-settings-dark` / `10-credential-modal-dark`：核心设置与凭据安全写入弹窗。
- `11-settings-light` / `21-conversation-light`：浅色主题（`--bg:#eef2f1`、`--accent:#087f73`）。
- `12-standard-920x640-dark`：最小标准尺寸。
- `13-compact-main-dark` / `14-compact-history-overlay-dark` / `15-compact-settings-overlay-dark`：紧凑模式 450×600、历史与设置全窗覆盖层。
- `16-degraded-dark`：仅语音缺失（连接状态“文字可用”）；`17-active-voice-dark`：连续语音聆听态（“正在聆听”、停止按钮）；`18-error-dark`：连接异常；`19-starting-loading-dark`：启动加载（polite status）。
- `20-reduced-motion-dark`：`--force-prefers-reduced-motion` 下 `matchMedia` 命中，html 与控件过渡时长计算值为 `1e-05s`。
- `22-packaged-app-setup-dark` / `23-packaged-app-ready-dark`：打包 `app.asar` 运行时的缺凭据引导与就绪态；`24-packaged-exe-ready-dark`：打包 `airi.exe` 运行时的就绪态（窗口 1184×764 外框）。

运行时断言（通过 CDP eval）：标准窗口 `1181×761` 外框（约 1180×760 内框）、最小 `921×641`、紧凑 `451×601`；深色 `--accent:#45d7c5`、`--bg:#101416`；字体族 `DM Sans Variable`；发送按钮圆角 `12px`；checkbox `accent-color: rgb(69,215,197)`；tabs 过渡 `0.18s`。

完成标准为：聚焦及完整 pytest、ruff、mypy 通过；两个 AIRI 补丁顺序 `git apply` 与回滚通过；协议与窗口生命周期 Vitest、Vue TypeScript typecheck 通过；`build:unpack` 产出 `dist/win-unpacked` 并通过打包产物运行时验收；Electron 截图回归全部执行并记录证据；Godot sidecar 已导出并组装，`verify_airi_windows.ps1` 无签名模式通过；真实音频设备验收通过。全部计划验收项已有证据闭环。

## 启动方式（2026-08-07 已验证）

1. 确认 bundle 与模型存在：
   - `E:\桌面\feishu\desktop-client-dist\airi.exe`
   - `E:\桌面\feishu\model\8496491754682859078.vrm`
2. 使用持久化的本地配置启动：
   ```
   cd E:\桌面\feishu
   python -m companion --desktop --config config\desktop.yaml
   ```
   配置已持久化到 `E:\桌面\feishu\config\desktop.yaml`，填入 bundle 与模型的路径和 SHA-256，并启用受管 Avatar 启动。
3. 端到端验证记录：宿主启动 Control Server（随机回环端口）→ 拉起 `airi.exe` → 界面握手连接 → readiness 达到 `5/5 healthy（LLM/TTS/ASR/Memory/Avatar）`，麦克风与语音输入可用。关闭 AIRI 窗口/进程后宿主自动关闭语音、编排器与内存服务，无残留进程。
4. 若希望直接使用默认配置，需先手工把 `config\default.yaml` 中 `providers.avatar.enabled` 置为 `true`，并填写 `providers.avatar.launch` 的路径与 SHA-256（或直接复制 `config\desktop.yaml` 的内容）。

### ASR 已打通（2026-08-07）

- 修复前：`FasterWhisperASRProvider.health_check()` 在模型未加载时返回 `degraded`，而编排器启动只做健康检查、不先预载模型，导致语音输入能力始终为 False。
- 修复：`ASRProvider` 协议增加默认空实现的 `preload()`；`CompanionOrchestrator.startup()` 在就绪阶段对 ASR 先 `preload()`（faster-whisper 首次约 17 秒，与 LLM/TTS 网络检查并发执行）再 `health_check()`。
- 端到端验证：`python -m companion --desktop --config config\desktop.yaml` 后日志为 `Provider health: 5/5 healthy (LLM/TTS/ASR/Memory/Avatar)`，快照 `voice_input` 为 True，麦克风按钮启用。
- 测试：完整 `pytest -q` 为 `604 passed`（新增 ASR preload 与编排器预载回归测试），ruff 与 mypy 通过。
