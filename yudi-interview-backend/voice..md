

根据我的分析，我来为你详细讲解语音面试模块的语音采集处理方式。

---

## 语音面试模块架构分析

### 一、是否使用了 WebRTC？

**答案：没有使用 WebRTC**。项目采用了 **WebSocket + 阿里云 DashScope 语音服务** 的架构。

---

### 二、用户语音采集处理流程

#### 前端采集流程 (`AudioRecorder.tsx`)

```
┌─────────────────────────────────────────────────────────────┐
│                      前端语音采集流程                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. navigator.mediaDevices.getUserMedia()                    │
│     ↓ 获取麦克风音频流                                        │
│     - echoCancellation: true (回声消除)                      │
│     - noiseSuppression: true (噪声抑制)                      │
│     - autoGainControl: true (自动增益控制)                   │
│     - sampleRate: 16000 Hz                                  │
│                                                             │
│  2. VAD (Voice Activity Detection)                         │
│     使用 @ricky0123/vad-web 库 + ONNX Runtime Web            │
│     - onSpeechStart: 检测到用户开始说话                       │
│     - onSpeechEnd: 检测到用户停止说话                         │
│                                                             │
│  3. AudioWorklet (pcm-processor.js)                        │
│     自定义音频处理器：                                        │
│     - 接收 Float32 格式的麦克风数据                          │
│     - 重采样到 16kHz                                         │
│     - Float32 → Int16 PCM 转换                              │
│     - 每 200ms 打包一个 chunk (3200 samples)                 │
│     - 通过 postMessage 发送到主线程                           │
│                                                             │
│  4. WebSocket 传输                                          │
│     - Base64 编码音频数据                                    │
│     - 发送到后端进行 ASR                                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

#### 关键代码片段

**麦克风采集配置** (`AudioRecorder.tsx:148-155`):

```typescript
const stream = await navigator.mediaDevices.getUserMedia({
  audio: {
    echoCancellation: true,      // 回声消除
    noiseSuppression: true,      // 噪声抑制
    autoGainControl: true,        // 自动增益控制
    sampleRate: TARGET_SAMPLE_RATE, // 16000 Hz
  },
});
```

**AudioWorklet 处理器** (`pcm-processor.js`):

```javascript
class PcmProcessor extends AudioWorkletProcessor {
  process(inputs, outputs, parameters) {
    const input = inputs[0]?.[0];
    if (!input || input.length === 0) return true;

    // 重采样到 16kHz
    const resampled = this.resample(input, sampleRate, 16000);
    // 转换为 Int16 PCM
    const pcm = this.float32ToInt16(resampled);
    this.enqueue(pcm);
    // 每 200ms 发送一个 chunk
    this.flushChunks();
    return true;
  }
}
```

---

### 三、后端语音处理流程

#### WebSocket Handler (`VoiceInterviewWebSocketHandler.java`)

```
┌─────────────────────────────────────────────────────────────────┐
│                      后端语音处理流程                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  用户音频 (Base64)                                                │
│        ↓                                                         │
│  ┌─────────────────┐                                             │
│  │  handleUserAudio │ ←── 丢弃 AI 说话期间的回声                    │
│  └────────┬────────┘        (800ms 冷却期)                        │
│           ↓                                                       │
│  ┌─────────────────┐                                             │
│  │  QwenAsrService │ ←── 阿里云 qwen3-asr-flash-realtime         │
│  │   (STT 服务)     │     - WebSocket 连接                         │
│  └────────┬────────┘     - 16kHz PCM 输入                         │
│           ↓              - 服务器端 VAD (400ms 静音检测)            │
│     STT 结果                                                        │
│     (partial/final)                                               │
│           ↓                                                       │
│  ┌─────────────────┐                                             │
│  │ SessionState    │ ←── 合并多段 STT 结果                         │
│  │ (mergeBuffer)   │     等待用户手动提交                           │
│  └────────┬────────┘                                             │
│           ↓                                                       │
│  ┌─────────────────┐                                             │
│  │ DashscopeLlmService │ ←── 调用 LLM 生成回复                     │
│  │   (对话服务)     │     (支持流式输出)                            │
│  └────────┬────────┘                                             │
│           ↓                                                       │
│  ┌─────────────────┐                                             │
│  │  QwenTtsService │ ←── 阿里云 TTS 合成                          │
│  │   (TTS 服务)     │     - 生成 AI 语音                           │
│  └────────┬────────┘     - 24kHz WAV 格式                        │
│           ↓                                                       │
│  WebSocket 发送音频 + 字幕到前端                                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

#### 完整对话流程 (`VoiceInterviewWebSocketHandler.java`)

```
1. afterConnectionEstablished()
   ├── 启动 DashScope ASR (startDashScopeStt)
   ├── 发送欢迎消息
   └── 触发开场白 (triggerOpeningQuestionIfNeeded)

2. handleTextMessage(audio)
   └── handleUserAudio()
       └── sttService.sendAudio() → 阿里云 ASR

3. handleSttResult() [回调]
   ├── partial → 实时字幕 (sendSubtitle, isFinal=false)
   └── final → 合并到 mergeBuffer (isFinal=true)

4. handleControl(submit)
   └── flushMergedUtteranceToLlm()
       └── triggerLlmResponse()
           ├── llmService.chatStreamSentences() → LLM
           ├── ttsService.synthesize() → TTS
           ├── saveMessage() → 数据库
           └── sendAudio() → 前端

5. afterConnectionClosed()
   ├── sttService.stopTranscription()
   └── interviewService.endSessionIfInProgress()
```

---

### 四、回声消除机制

系统采用**多层次回声消除**策略：

```typescript
// 前端：GainNode 静音
const gainNode = audioContext.createGain();
gainNode.gain.value = 0;  // 播放时静音，防止扬声器声音进入麦克风
gainNodeRef.current = gainNode;
source.connect(workletNode);
workletNode.connect(gainNode);
gainNode.connect(audioContext.destination);
```

```java
// 后端：AI 说话期间丢弃麦克风输入
// VoiceInterviewWebSocketHandler.java:487-491
if (state.isAiSpeakingOrCooldown()) {
    return;  // AI 正在说话或处于 800ms 冷却期时，丢弃音频
}

// SessionState.java:1477-1483
boolean isAiSpeakingOrCooldown() {
    if (aiSpeaking.get()) {
        return true;
    }
    // AI 播放结束后的冷却期（默认 800ms）
    return System.currentTimeMillis() < aiSpeakEndAt.get();
}
```

---

### 五、音频格式总结

| 参数 | 值 |
|------|-----|
| 采样率 | 16kHz (前端) / 24kHz (TTS 输出) |
| 位深 | 16-bit |
| 声道 | 单声道 (mono) |
| 格式 | PCM (Int16) → WAV (后端) |
| 传输 | Base64 编码，通过 WebSocket |
| 块大小 | 每 200ms 一个 chunk (3200 samples) |

---

### 六、与 WebRTC 的区别

| 特性 | 本项目方案 (WebSocket) | WebRTC |
|------|----------------------|--------|
| 连接方式 | 客户端 ↔ 服务器 ↔ 阿里云 | 点对点 |
| 音频处理 | 服务器端 ASR/TTS | 客户端本地处理 |
| 实时性 | 依赖网络延迟 | 更低延迟 |
| 复杂度 | 较低 | 较高 (STUN/TURN/ICE) |
| 费用 | 云服务费用 | P2P 无额外费用 |
| 适用场景 | AI 对话面试 | 实时通话/会议 |

这个架构非常适合 AI 面试场景，因为所有语音处理都由服务器端的阿里云服务完成，前端只需要负责采集和播放。