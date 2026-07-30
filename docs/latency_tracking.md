# 延迟追踪规范（Latency Tracking Spec）

> 版本: 0.1.0 | 对应 Phase 0 交付要求

## 1. 追踪目标

根据 PLAN Section 2，核心延迟指标：

| 指标 | 目标 (p50) | 目标 (p95) | 测量方法 |
|---|---|---|---|
| 首音频响应 | < 900 ms | < 1.8 s | VAD final → 首音频字节播放 |
| VAD 触发 | < 200 ms | < 500 ms | 语音结束 → VAD 判定 |
| ASR 最终文本 | < 300 ms | < 800 ms | VAD 触发 → final transcript |
| LLM 首 Token | < 500 ms | < 1.5 s | ASR final → 首个 LLM token |
| LLM 完整响应 | < 2 s | < 5 s | ASR final → LLM response complete |
| TTS 首字节 | < 300 ms | < 800 ms | 文本输入 → 首个音频字节 |
| 打断响应 | < 200 ms | < 300 ms | 中断信号 → 播放停止 |

当前免费 Fish `s2.1-pro-free` 接入阶段，`providers.asr.capture.target_e2e_latency_ms`
临时放宽为 30,000 ms，只用于验证真实麦克风、faster-whisper、DeepSeek、Fish TTS、播放和
barge-in 链路已打通。上表仍是付费低延迟模型/生产体验目标；切换到有 SLA 的 Fish 模型后需把
验收阈值重新收紧并重新采集目标机器证据。

## 2. 追踪点位

系统中的关键追踪时间点（使用 OpenTelemetry spans）：

```
[麦克风] → [VAD] → [ASR] → [LLM] → [TTS] → [扬声器]
   |          |       |       |        |         |
   t0        t1      t2      t3       t4        t5

t0: 音频帧捕获 (audio_captured)
t1: VAD 检测到语音结束 (vad_speech_end)
t2: ASR 产生最终文本 (asr_final_text)
t3: LLM 首 Token (llm_first_token)
t4: LLM 响应完成 (llm_response_complete)
t5: TTS 首音频字节 (tts_first_byte)
t6: 音频开始播放 (audio_playback_start)
t7: 音频播放完成 (audio_playback_end)
t8: 中断信号 (interrupt_signal)
t9: 播放停止 (playback_stopped)
```

## 3. 指标计算

### 端到端首音频延迟
```
e2e_latency = t6 - t1
目标: p50 < 900ms, p95 < 1.8s（免费 Fish 连通性验收临时阈值为 30,000ms）
```

### ASR 延迟
```
asr_latency = t2 - t1
目标: p50 < 300ms, p95 < 800ms
```

### LLM 首 Token 延迟
```
llm_ttft = t3 - t2
目标: p50 < 500ms, p95 < 1.5s
```

### TTS 首字节延迟
```
tts_ttfb = t5 - t4
目标: p50 < 300ms, p95 < 800ms
```

### 打断响应延迟
```
interrupt_latency = t9 - t8
目标: p50 < 200ms, p95 < 300ms
```

## 4. 追踪数据格式

```json
{
  "trace_id": "0af7651916cd43dd8448eb211c80319c",
  "turn_id": "turn_01HXYZ",
  "spans": [
    {
      "name": "voice_pipeline",
      "start_time": "2026-07-28T20:31:10.000+08:00",
      "end_time": "2026-07-28T20:31:11.200+08:00",
      "attributes": {
        "vad_latency_ms": 180,
        "asr_latency_ms": 250,
        "llm_ttft_ms": 400,
        "tts_ttfb_ms": 280,
        "e2e_latency_ms": 1110,
        "interrupted": false,
        "model_id": "claude-sonnet-5",
        "tts_provider": "azure",
        "asr_provider": "azure"
      }
    }
  ]
}
```

## 5. 异常处理

| 情况 | 处理方式 |
|---|---|
| 网络超时 | 标记 span 为 error，记录超时类型 |
| 模型返回错误 | 记录错误码和 fallback 行为 |
| 打断发生 | 当前 span 记录 interrupted=true，截断时间 |
| TTS 生成失败 | 记录 tts_error，如有 fallback TTS 则记录二次延迟 |
| 空音频 | 标记 turn 为 silent_turn，不计入延迟统计 |

## 6. 可视化要求

- 每段延迟的 p50/p95/p99 分布
- 按 provider 分组的延迟对比
- 按小时/天的时间趋势
- 打断率 vs 延迟的散点图
- 网络延迟 vs 模型延迟的堆叠图
