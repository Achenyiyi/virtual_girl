# 二次元虚拟伴侣 (Virtual Companion Runtime)

一个本地优先、云端增强、模块可替换、以事件日志为事实源的虚拟生命运行时系统。

> 🚧 **预发布验证中** — Avatar 真实舞台与 Windows 安装链已验证；真实语音端到端、
> 正式 Authenticode 签名和最终发行签字仍待完成

## 项目目标

构建一个接近《命运石之门》Amadeus 或现代 AI VTuber 的"持续存在感"体验，而不是一个带立绘的聊天框。

### 核心创新

将"活着感"定义为四种连连续性，并分别实现和评测：
1. **对话节奏连续性** — 流式、可打断、自然接话
2. **记忆因果连续性** — 记得事件时间、人物关系、偏好变化
3. **情绪状态连续性** — 情绪有惯性、原因和恢复过程
4. **环境行为连续性** — 在合适时机主动回应屏幕事件和计划

## 架构概览

```
Avatar Stage ← Authenticated WebSocket Bridge ← CompanionOrchestrator → Model Router
                                                   ↕              ↕
                                              StateManager    Memory Service
                                                   ↕              ↕
                                              Policy Gate     Perception
                                                   ↕
                                              Action Service
```

## 项目结构

```
companion/
├── events/        # 领域事件Schema（生命事件账本）
├── providers/     # 可替换Provider接口（LLM/TTS/ASR/记忆/角色/行动/感知）
├── core/          # 核心编排（Orchestrator、EventBus、PolicyGate、StateManager）
├── memory/        # 五层记忆系统实现（SQLite+FTS5）
├── protocols/     # 通信协议（Turn管理、音频确认）
├── schemas/       # 数据Schema（身份、关系、情绪、行动分类）
tests/             # 测试套件
config/            # 配置文件
docs/              # 文档
```

## 快速开始

### 环境要求
- Windows 11 x64
- Python 3.12+
- RTX 3060 或以上（可选，用于本地模型）

### 安装

```bash
# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 开发、测试和发布工具依赖
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .

# 仅运行应用或验证发布 wheel 时，改用更小的运行时锁
# pip install --require-hashes -r requirements-runtime.lock
# pip install --no-deps virtual_companion-0.1.0-py3-none-any.whl

# 运行测试
pytest tests/ -v
```

更新依赖后，使用发布工具组重新生成带哈希的锁文件：

```bash
pip install -e ".[release]"
pip-compile --extra voice --extra dev --extra release --strip-extras \
  --generate-hashes --allow-unsafe \
  --no-emit-index-url --output-file requirements.lock pyproject.toml
pip-compile --constraint requirements.lock --extra voice --strip-extras \
  --generate-hashes --allow-unsafe \
  --no-emit-index-url --output-file requirements-runtime.lock pyproject.toml
```

普通 Windows 用户不需要预装 Python。正式 Release 提供的签名安装包会携带固定的
CPython 3.12.10、仅运行时依赖、AIRI 舞台和经许可审核的 VRM；完整构建与验收流程见
[`docs/release_process.md`](docs/release_process.md)。在正式签名 Release 发布前，不要把 CI
中的 Python wheel/sdist 当作面向最终用户的桌面安装包。

开发或一次性验收可通过环境变量临时注入云端凭据：

```powershell
$env:DEEPSEEK_API_KEY = "..."
$env:FISH_API_KEY = "..."
python -m companion --voice-input
```

生产桌面环境建议改用 Windows“通用凭据”，默认目标为
`VirtualCompanion/DeepSeek`、`VirtualCompanion/FishAudio` 和
`VirtualCompanion/AvatarBridge`。环境变量会优先覆盖已保存凭据；不要把 Key 写入 YAML、
`.env`、key 文件、命令参数或启动脚本。设置与轮换步骤见
[`docs/deployment_preflight.md`](docs/deployment_preflight.md)。

Fish Audio TTS 当前使用官方免费开放的 `s2.1-pro-free` 模型，默认目标声音为“AD学姐”；本地 ASR 仍使用
faster-whisper。语音回复使用官方 `/v1/tts/stream/with-timestamp` SSE 接口，将 LLM 的安全完整
分句尽早送入 24 kHz PCM 合成，并用时间戳 alignment 只提交实际播放到的文本。默认参数面向
对话体验：`latency: balanced`、较短 `chunk_length`/`min_chunk_length`、跨块上下文一致性，
并把长回复拆成约 480 bytes 的 Fish 文本片段，以降低首音等待、提高取消响应并缩小单次请求
故障范围；完整回复仍会全部分段合成，不会截断。上线前仍需把任何曾出现在聊天、日志或命令中
的 Fish/DeepSeek 密钥轮换为从未披露的新值。

Avatar Bridge 使用本机随机 token，可由程序直接安全初始化且不会回显：

```powershell
python -m companion --config production.yaml --provision-avatar-token
```

配置文件采用严格字段校验。未知字段或拼写错误会在任何 Provider 启动前直接报错，避免安全、
超时、审计或主动策略选项被静默忽略并退回默认值。

同一 Windows 登录会话中，一个伴侣资料库只允许一个运行实例。第二个常驻实例或 Avatar
验收进程会在打开 Provider、麦克风和舞台前退出；并行开发实例必须使用独立的
`providers.memory.db_path` 和运行目录。

正式运行要求记忆库、日志和行动审计库位于本地 Windows 卷、目标可真实写入且所在卷至少
保留 512 MiB 可用空间。不要把实时 SQLite/WAL 数据库直接放到 UNC、网络映射盘或同步盘；
需要异地保存时使用在线备份文件。安装版配置应设置 `runtime.data_root: user_local`，使这些
可写数据进入 `%LOCALAPPDATA%\VirtualCompanion`；AIRI 与模型路径仍从配置文件所在的只读
安装目录解析。

### 备份记忆

运行中数据库使用 SQLite 在线备份 API 创建一致性快照；默认拒绝覆盖已有备份：

```powershell
python -m companion --backup-memory D:\CompanionBackups\memory-2026-07-29.db
python -m companion --verify-memory-backup D:\CompanionBackups\memory-2026-07-29.db
python -m companion --restore-memory-backup D:\CompanionBackups\memory-2026-07-29.db

# 仅在明确需要替换同名备份时
python -m companion --backup-memory D:\CompanionBackups\latest.db --overwrite-backup
```

备份完成前使用随机临时文件，校验 SQLite 完整性和必要表后再原子发布。备份目录应位于
独立磁盘或受控同步位置，不要只保存在运行数据库旁边。

记忆库使用 SQLite `application_id` 和 `user_version` 标识所有权与结构版本。首次启动会
无损登记完整的旧版无标记记忆库；不相关、结构残缺或高于当前程序版本的数据库会在
启动和 `--doctor` 阶段拒绝，避免误写或静默降级。旧版无标记备份仍可独立校验。

首次启用 `--voice-input` 时，faster-whisper 会加载配置中的本地模型。

启动前建议先运行不会输出凭据内容的结构化自检：

```powershell
# 只校验字段、类型、范围和安全约束，不访问凭据、存储、设备或 Provider
python -m companion --config production.yaml --validate-config

# 本地核心检查
python -m companion --doctor

# 包含语音依赖、默认麦克风和播放设备
python -m companion --doctor --voice-input

# 显式深度检查：加载/推理 Whisper，打开真实麦克风与静音播放流
python -m companion --doctor-voice-hardware

# 凭据注入后验证远程 LLM/TTS；健康探针不会生成收费语音
python -m companion --doctor-online --voice-input

# 自动化消费
python -m companion --doctor-json --voice-input

# 轮换凭据注入后的真实语音上线验收；交互提示走 stderr，结果写入 JSON
python -m companion --accept-voice-json 1>voice-acceptance.json

# AIRI/Live2D/VRM 扩展启动后，验收模型加载、状态应用和真实渲染帧推进
python -m companion --config production.yaml --accept-avatar-json 1>avatar-acceptance.json
```

语音上线验收要求一轮真实麦克风到流式播放完整成功，并证明首段音频在 LLM 流结束前播放、
所有 PCM 使用同一声卡流且没有 underflow、完成轮历史只含实际播放文本；随后在第二轮播放期间
通过新的 VAD speech-start 边沿立即打断，并证明打断轮没有保存未播放文本。当前免费 Fish
模型没有 SLA/TTFA 保证，默认门槛为首音频 30,000 ms、打断 300 ms。未来切换付费模式只需
替换 Fish `model` 名称，并用期望的更低延迟阈值重新采集上线证据。完整流程和报告字段见
[`docs/deployment_preflight.md`](docs/deployment_preflight.md)。

形象舞台上线验收要求启用 `providers.avatar`、设置 `identity.avatar_model_id` 和头像鉴权
环境变量。舞台扩展还需实现只读 `stage.inspect` 验收方法，报告渲染器实际应用的状态序号
和已呈现帧序号；具体字段及人工视觉签字要求见
[`docs/avatar_bridge_protocol.md`](docs/avatar_bridge_protocol.md) 和上线预检文档。

本机用户模型 `model/8496491754682859078.vrm` 的固定 ID、大小和 SHA-256 记录在
[`integrations/airi-v0.11.3/managed-avatar.json`](integrations/airi-v0.11.3/managed-avatar.json)。
模型二进制仍是本地用户资产并被 Git 忽略；受管启动通过 `model_path`、哈希和固定 ID
校验后自动注册、选择该 VRM，无需在 AIRI 里手动导入。模型内嵌的 VRoid Hub 许可允许
企业/个人商用、重新分发和修改且无需署名；许可证据、精确哈希和未来替换约束见
[`docs/third_party_assets.md`](docs/third_party_assets.md)。

形象舞台默认关闭。桥接协议、鉴权环境变量和 AIRI 当前适配状态见
[`docs/avatar_bridge_protocol.md`](docs/avatar_bridge_protocol.md)。仅在 AIRI、`app.asar`、Godot
sidecar 和 VRM 的路径与哈希均已固定，且 Avatar token 已进入安全凭据源后，才在生产配置中把
`providers.avatar.enabled` 设为 `true`。

Windows 行动能力同样默认关闭。目前只提供三个不可变、无参数的只读诊断能力；启用方式、隐私
影响和明确不支持的操作见
[`docs/windows_readonly_actions.md`](docs/windows_readonly_actions.md)。

默认文件日志按 10 MiB 轮转并保留 5 个历史文件；事件重放和诊断历史仅保留有界内存窗口，完整
会话事件仍写入 SQLite 账本。可通过 `dev.log_max_bytes`、`dev.log_backup_count` 和
`dev.event_log_retention` 调整，但超出安全范围的配置会被拒绝。

## 开发阶段

| Phase | 目标 | 状态 |
|---|---|---|
| Phase 0 | 事件 Schema、Provider 接口、协议、威胁模型 | 🟢 本地与远程 CI 门禁通过 |
| Phase 1 | 可打断 ASR→LLM→流式 TTS→播放 | 🟡 自动化门禁通过，增强后的真实设备验收待采集 |
| Phase 2 | 事件账本、五层记忆、时间化事实 | 🟢 自动化验证通过 |
| Phase 3 | 连续情绪状态、表情/动作/TTS一致映射 | 🟢 AIRI 真实模型、表情、动作和呈现帧验收通过 |
| Phase 4 | 主动预算、计划/反思、受控电脑工具 | 🟡 Windows 只读能力已验证，写操作保持禁用 |
| Phase 5 | 纵向实验、全双工升级 | ⬜ 待开始 |

## 关键设计原则

1. **事件日志是事实源** — 所有派生记忆必须能从原始事件重建
2. **LLM只提候选，策略门做决策** — 主动性/安全性由确定性规则控制
3. **人格稳定内核+可成长关系层** — 防止"每次对话都是新角色"
4. **本地优先，云端增强** — 隐私数据不离开本机
5. **所有写操作经权限分类** — 只读自动/低风险策略/高风险确认

## 许可证

MIT
