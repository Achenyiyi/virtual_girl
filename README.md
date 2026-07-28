# 二次元虚拟伴侣 (Virtual Companion Runtime)

一个本地优先、云端增强、模块可替换、以事件日志为事实源的虚拟生命运行时系统。

> 🚧 **预发布验证中** — 自动化门禁已建立，真实设备与外部舞台验收仍未完成

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

# 安装依赖
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
pip install -e ".[dev]"

# 需要本地语音输入/流式播放时
# requirements.lock 已包含 Windows 语音运行时依赖

# 运行测试
pytest tests/ -v
```

更新依赖后，使用发布工具组重新生成带哈希的锁文件：

```bash
pip install -e ".[release]"
pip-compile --extra voice --strip-extras --generate-hashes --allow-unsafe \
  --no-emit-index-url --output-file requirements.lock pyproject.toml
```

运行前通过环境变量注入云端凭据；不要把 Key 写入项目文件：

```powershell
$env:DEEPSEEK_API_KEY = "..."
$env:AZURE_SPEECH_KEY = "..."
python -m companion --voice-input
```

首次启用 `--voice-input` 时，faster-whisper 会加载配置中的本地模型。

启动前建议先运行不会输出凭据内容的结构化自检：

```powershell
# 本地核心检查
python -m companion --doctor

# 包含语音依赖、默认麦克风和播放设备
python -m companion --doctor --voice-input

# 凭据注入后验证远程 LLM/TTS；健康探针不会生成收费语音
python -m companion --doctor-online --voice-input

# 自动化消费
python -m companion --doctor-json --voice-input
```

形象舞台默认关闭。桥接协议、鉴权环境变量和 AIRI 当前适配状态见
[`docs/avatar_bridge_protocol.md`](docs/avatar_bridge_protocol.md)。在真实舞台扩展完成前，不要把
`providers.avatar.enabled` 设为 `true`。

Windows 行动能力同样默认关闭。目前只提供三个不可变、无参数的只读诊断能力；启用方式、隐私
影响和明确不支持的操作见
[`docs/windows_readonly_actions.md`](docs/windows_readonly_actions.md)。

## 开发阶段

| Phase | 目标 | 状态 |
|---|---|---|
| Phase 0 | 事件 Schema、Provider 接口、协议、威胁模型 | 🟡 本地门禁通过，远程 CI 待跑 |
| Phase 1 | 可打断 ASR→LLM→流式 TTS→播放 | 🟡 代码完成，真实设备验收待做 |
| Phase 2 | 事件账本、五层记忆、时间化事实 | 🟢 自动化验证通过 |
| Phase 3 | 连续情绪状态、表情/动作/TTS一致映射 | 🟡 桥接和映射已验证，AIRI 舞台扩展待做 |
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
